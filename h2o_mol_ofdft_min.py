import os
import json
import argparse
from functools import partial
from typing import Any, Union
import pandas as pd

import jax
from jax import lax, vmap, numpy as jnp
import jax.random as jrnd
from jax._src import prng

from flax.training import checkpoints
import optax
from distrax import MultivariateNormalDiag, Laplace

from ofdft_normflows.functionals import _kinetic, _nuclear, _hartree, _exchange, cusp_condition
from ofdft_normflows.dft_distrax import DFTDistribution
from ofdft_normflows.cn_flows import neural_ode, neural_ode_score
from ofdft_normflows.cn_flows import Gen_CNFSimpleMLP as CNF
from ofdft_normflows.cn_flows import Gen_CNFRicky as CNFRicky
from ofdft_normflows.utils import get_scheduler

import matplotlib.pyplot as plt

Array = Any
KeyArray = Union[Array, prng.PRNGKeyArray]

jax.config.update("jax_enable_x64", True)
# jax.config.update('jax_disable_jit', True)

BHOR = 1.8897259886  # 1AA to BHOR
# CKPT_DIR = "Results/GP_pot"
# FIG_DIR = "Figures/GP_pot"


@ partial(jax.jit,  static_argnums=(2,))
def compute_integral(params: Any, grid_array: Any, rho: Any, Ne: int, bs: int):
    grid_coords, grid_weights = grid_array
    rho_val = Ne*rho(params, grid_coords)
    return jnp.vdot(grid_weights, rho_val)


def training(t_kin: str = 'TF',
             v_pot: str = 'HGH',
             h_pot: str = 'MT',
             x_pot: str = 'Dirac',
             batch_size: int = 256,
             epochs: int = 100,
             lr: float = 1E-5,
             nn_arch: tuple = (512, 512,),
             bool_load_params: bool = False,
             scheduler_type: str = 'ones'):

    mol_name = 'H2O'
    Ne = 10
    # O	0.0000000	0.0000000	0.1189120
    # H	0.0000000	0.7612710	-0.4756480
    # H	0.0000000	-0.7612710	-0.4756480
    coords = jnp.array([[0.0,	0.0,	0.1189120],
                        [0.0,	0.7612710,	-0.4756480],
                        [0.0,	-0.7612710,	-0.4756480]])*BHOR
    z = jnp.array([[8.], [1.], [1.]])
    atoms = ['O', 'H', 'H']
    mol = {'coords': coords, 'z': z}

    png = jrnd.PRNGKey(0)
    _, key = jrnd.split(png)

    model_rev = CNF(3, nn_arch, bool_neg=False)
    model_fwd = CNF(3, nn_arch, bool_neg=True)
    test_inputs = lax.concatenate((jnp.ones((1, 3)), jnp.ones((1, 1))), 1)
    params = model_rev.init(key, jnp.array(0.), test_inputs)

    @jax.jit
    def NODE_rev(params, batch): return neural_ode(
        params, batch, model_rev, -1., 0., 3)

    @jax.jit
    def NODE_fwd(params, batch): return neural_ode(
        params, batch, model_fwd, 0., 1., 3)

    @jax.jit
    def NODE_fwd_score(params, batch): return neural_ode_score(
        params, batch, model_fwd, 0., 1., 3)

    mean = jnp.zeros((3,))
    cov = jnp.ones((3,))
    prior_dist = MultivariateNormalDiag(mean, cov)

    m = DFTDistribution(atoms, coords)
    normalization_array = (m.coords, m.weights)

    # optimizer = optax.adam(learning_rate=1E-3)
    lr_sched = get_scheduler(epochs, scheduler_type, lr)
    optimizer = optax.chain(
        optax.clip_by_global_norm(1.0),
        optax.rmsprop(learning_rate=lr_sched)
        # optax.adam(learning_rate=lr_sched),
    )
    opt_state = optimizer.init(params)

    # load prev parameters
    if bool_load_params:
        restored_state = checkpoints.restore_checkpoint(
            ckpt_dir=CKPT_DIR, target=params, step=0)
        params = restored_state

    @jax.jit
    def rho_x(params, samples):
        zt, logp_zt = NODE_fwd(params, samples)
        return jnp.exp(logp_zt), zt, None

    @jax.jit
    def rho_x_score(params, samples):
        zt, logp_zt, score_zt = NODE_fwd_score(params, samples)
        return jnp.exp(logp_zt), zt, score_zt

    @jax.jit
    def rho_rev(params, x):
        zt = lax.concatenate((x, jnp.zeros((x.shape[0], 1))), 1)
        z0, logp_z0 = NODE_rev(params, zt)
        logp_x = prior_dist.log_prob(z0)[:, None] - logp_z0
        return jnp.exp(logp_x)  # logp_x

    @jax.jit
    def T(params, samples):
        zt, _ = NODE_fwd(params, samples)
        return zt

    t_functional = _kinetic(t_kin)
    v_functional = _nuclear(v_pot)
    vh_functional = _hartree(h_pot)
    x_functional = _exchange(x_pot)

    @jax.jit
    def loss(params, u_samples):
        # den_all, x_all = rho_and_x(params, u_samples)
        den_all, x_all, score_all = rho_x_score(params, u_samples)

        den, denp = den_all[:batch_size], den_all[batch_size:]
        x, xp = x_all[:batch_size], x_all[batch_size:]
        score, scorep = score_all[:batch_size], score_all[batch_size:]

        e_t = t_functional(den, score, Ne)
        e_h = vh_functional(x, xp, Ne)
        e_nuc_v = v_functional(x, Ne, mol)
        e_x = x_functional(den, Ne)

        # cusp = cusp_condition(params, rho_rev, mol)
        # cusp = 0.

        e = e_t + e_nuc_v + e_h + e_x  # + cusp
        energy = jnp.mean(e)
        return energy, {"t": jnp.mean(e_t),
                        "v": jnp.mean(e_nuc_v),
                        "h": jnp.mean(e_h),
                        "x": jnp.mean(e_x),
                        "e": energy}
        # "cusp": cusp}

    @jax.jit
    def step(params, opt_state, batch):
        loss_value, grads = jax.value_and_grad(
            loss, has_aux=True)(params, batch)
        updates, opt_state = optimizer.update(grads, opt_state, params)
        params = optax.apply_updates(params, updates)
        return params, opt_state, loss_value

    def batches_generator(key: prng.PRNGKeyArray, batch_size: int):
        v_score = vmap(jax.jacrev(lambda x:
                                  prior_dist.log_prob(x)))
        while True:
            _, key = jrnd.split(key)
            samples = prior_dist.sample(seed=key, sample_shape=batch_size)
            logp_samples = prior_dist.log_prob(samples)
            score = v_score(samples)
            samples0 = lax.concatenate(
                (samples, logp_samples[:, None], score), 1)

            _, key = jrnd.split(key)
            samples = prior_dist.sample(seed=key, sample_shape=batch_size)
            logp_samples = prior_dist.log_prob(samples)
            score = v_score(samples)
            samples1 = lax.concatenate(
                (samples, logp_samples[:, None], score), 1)

            yield lax.concatenate((samples0, samples1), 0)

    loss0 = jnp.inf
    df = pd.DataFrame()
    _, key = jrnd.split(key)
    gen_batches = batches_generator(key, batch_size)

    for i in range(epochs+1):
        # ci = scheduler(i)
        batch = next(gen_batches)
        params, opt_state, loss_value = step(params, opt_state, batch)  # , ci
        loss_epoch, losses = loss_value

        norm_val = compute_integral(
            params, normalization_array, rho_rev, Ne, 0)

        # norm_integral, log_det_Jac = _integral(params)
        mean_energy = losses['e']
        r_ = {'epoch': i,
              'L': loss_epoch, 'E': mean_energy,
              'T': losses['t'], 'V': losses['v'], 'H': losses['h'], 'X': losses['x'],
              #   'cusp': losses['cusp'],
              'I': norm_val,
              # 'ci': ci
              }
        df = pd.concat([df, pd.DataFrame(r_, index=[0])], ignore_index=True)
        df.to_csv(
            f"{CKPT_DIR}/training_trajectory_{mol_name}.csv", index=False)

        if i % 5 == 0:
            _s = f"step {i}, L: {loss_epoch:.3f}, E:{mean_energy:.3f}\
            T: {losses['t']:.5f}, V: {losses['v']:.5f}, H: {losses['h']:.5f}, X: {losses['x']:.5f}, I: {norm_val:.4f}"  # , cusp: {losses['cusp']:.5f}
            print(_s,
                  file=open(f"{CKPT_DIR}/loss_epochs_{mol_name}.txt", 'a'))

        if loss_epoch < loss0:
            params_opt, loss0 = params, loss_epoch
            # checkpointing model model
            checkpoints.save_checkpoint(
                ckpt_dir=CKPT_DIR, target=params, step=0, overwrite=True)
            bool_plot = True
        else:
            bool_plot = False

        if i % 10 == 0 or i <= 50 or bool_plot:
            # 2D figure
            z = jnp.linspace(-5.25, 5.25, 128)
            x = 0.  # 0.699199  # H covalent radius
            yy, zz = jnp.meshgrid(z, z)
            X = jnp.array(
                [x*jnp.ones_like(yy.ravel()), yy.ravel(),  zz.ravel()]).T
            rho_pred = Ne*rho_rev(params, X)

            # exact density DFT
            if i == 0:
                rho_exact_2D = m.prob(m, X)

            vmin = jnp.min(jnp.stack([rho_exact_2D.ravel(), rho_pred.ravel()]))
            vmax = jnp.max(jnp.stack([rho_exact_2D.ravel(), rho_pred.ravel()]))
            # fig, ax = plt.subplots()
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 5))
            # plt.clf()
            ax1.set_title(
                f'epoch {i}, L = {loss_epoch:.3f}, E = {mean_energy:.3f}, I = {norm_val:.3f}')
            contour1 = ax1.contourf(
                yy, zz, rho_pred.reshape(yy.shape), levels=25,  vmin=vmin, vmax=vmax)
            # label=r'$N_{e}\;\rho_{NF}(x)$')
            cbar = fig.colorbar(contour1, ax=ax1)
            cbar.set_label(r'$N_{e}\rho_{NF}(x)$')

            contour2 = ax2.contourf(yy, zz, rho_exact_2D.reshape(xx.shape),
                                    linestyles='dashed', levels=25, vmin=vmin, vmax=vmax)
            #    label=r"$\hat{\rho}_{DFT}(x)$")

            cbar = fig.colorbar(contour2, ax=ax2)
            cbar.set_label(r'$\rho(x)$')

            ax1.scatter(coords[:, 1], coords[:, 2],
                        marker='o', color='k', s=35, zorder=2.5)
            ax2.scatter(coords[:, 1], coords[:, 2],
                        marker='o', color='k', s=35, zorder=2.5)
            ax1.set_xlabel('Y [Bhor]')
            ax1.set_ylabel('Z [Bhor]')
            ax2.set_xlabel('Y [Bhor]')
            ax2.set_ylabel('Z [Bhor]')
            # plt.legend()
            plt.tight_layout()
            plt.savefig(f'{FIG_DIR}/epoch_rho_xz_{i}.png')


def main():
    parser = argparse.ArgumentParser(description="Density fitting training")
    parser.add_argument("--epochs", type=int,
                        default=1, help="training epochs")
    parser.add_argument("--bs", type=int, default=12, help="batch size")
    parser.add_argument("--params", type=bool, default=False,
                        help="load pre-trained model")
    parser.add_argument("--lr", type=float, default=3E-4,
                        help="learning rate")
    parser.add_argument("--kin", type=str, default='tf-w',
                        help="Kinetic energy funcitonal")
    parser.add_argument("--nuc", type=str, default='HGH',
                        help="Nuclear Potential energy funcitonal")
    parser.add_argument("--hart", type=str, default='MT',
                        help="Hartree energy funcitonal")
    parser.add_argument("--x", type=str, default='Dirac',
                        help="Exchange energy funcitonal")
    # parser.add_argument("--N", type=int, default=1, help="number of particles")
    parser.add_argument("--sched", type=str, default='const',
                        help="Hartree integral scheduler")
    # parser.add_argument("--sched", type=str, default='one',
    #                     help="Hartree integral scheduler")
    args = parser.parse_args()

    batch_size = args.bs
    epochs = args.epochs
    bool_params = args.params
    lr = args.lr
    sched_type = args.sched

    kin = args.kin
    v_pot = args.nuc
    h_pot = args.hart
    x_pot = args.x
    nn = (512, 512, 512,)
    # Ne = args.N
    # scheduler_type = args.sched

    global CKPT_DIR
    global FIG_DIR
    CKPT_DIR = f"Results/H2O_{kin.upper()}_{v_pot.upper()}_{h_pot.upper()}_{x_pot.upper()}_lr_{lr:.1e}"
    FIG_DIR = f"{CKPT_DIR}/Figures"

    cwd = os.getcwd()
    rwd = os.path.join(cwd, CKPT_DIR)
    if not os.path.exists(rwd):
        os.makedirs(rwd)
    fwd = os.path.join(cwd, FIG_DIR)
    if not os.path.exists(fwd):
        os.makedirs(fwd)

    job_params = {'epohs': epochs,
                  'batch_size': batch_size,
                  'lr': lr,
                  'kin': kin,
                  'v_nuc': v_pot,
                  'h_pot': h_pot,
                  'x_pot': x_pot,
                  'nn': tuple(nn),
                  'sched': sched_type,
                  }
    with open(f"{CKPT_DIR}/job_params.json", "w") as outfile:
        json.dump(job_params, outfile, indent=4)

    # assert 0
    training(kin, v_pot, h_pot, x_pot, batch_size,
             epochs, lr, nn, bool_params, sched_type)


if __name__ == "__main__":
    main()