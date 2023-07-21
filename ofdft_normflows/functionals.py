from typing import Any, Callable
from functools import partial

import jax
import jax.numpy as jnp
from jax import jit, vmap, hessian, jacrev, lax

Array = jax.Array
BHOR = 1.  # 1.8897259886  # 1AA to BHOR


@partial(jit,  static_argnums=(2,))
def laplacian(params: Any, X: Array, fun: callable) -> jax.Array:
    """_summary_

    Args:
        X (Array): _description_
        params (Any): _description_
        fun (callable): _description_

    Returns:
        jax.Array: _description_
    """
    @partial(jit,  static_argnums=(2,))
    def _laplacian(params: Any, X: Array, fun: callable):
        hes_ = hessian(fun, argnums=1)(
            params, X[jnp.newaxis], )  # R[jnp.newaxis]
        hes_ = jnp.squeeze(hes_, axis=(0, 2, 4))
        hes_ = jnp.einsum('...ii', hes_)
        return hes_

    v_laplacian = vmap(_laplacian, in_axes=(None, 0,  None))
    return v_laplacian(params, X, fun)


@partial(jit,  static_argnums=(2,))
def score(params: Any, X: Array, fun: callable) -> jax.Array:

    @jit
    def _score(params: Any, xi: Array):
        score_ = jax.jacrev(fun, argnums=1)(params, xi[jnp.newaxis])
        return jnp.reshape(score_, xi.shape[0])

    v_score = vmap(_score, in_axes=(None, 0))
    return v_score(params, X)


# ------------------------------------------------------------------------------------------------------------
# POTENTIAL FUNCTIONALS
# ------------------------------------------------------------------------------------------------------------

@partial(jit,  static_argnums=(2,))
def harmonic_potential(params: Any, u: Any, T: Callable, k: Any = 1.) -> jax.Array:
    x, _ = T(params, u)
    return 0.5*k*jnp.mean(x**2)


@partial(jit,  static_argnums=(2,))
def dirac_exchange(params: Any, u: Any, rho: Callable) -> jax.Array:
    rho_val = rho(params, u)

    l = -(3/4)*(3/jnp.pi)**(1/3)
    return l*jnp.mean(rho_val**(1/3))

# ------------------------------------------------------------------------------------------------------------


# ------------------------------------------------------------------------------------------------------------
# KINETIC FUNCTIONALS
# ------------------------------------------------------------------------------------------------------------

def _kinetic(name: str = 'TF'):
    if name.lower() == 'tf' or name.lower() == 'thomas_fermi':
        def wrapper(*args):
            return thomas_fermi(*args)
    elif name.lower() == 'tf1d' or name.lower() == 'thomas_fermi_1d':
        def wrapper(*args):
            return thomas_fermi_1D(*args)
    elif name.lower() == 'w' or name.lower() == 'weizsacker':
        def wrapper(*args):
            return weizsacker(*args)
    elif name.lower() == 'k' or name.lower() == 'kinetic':
        def wrapper(*args):
            return kinetic(*args)
    return wrapper


@ partial(jit,  static_argnums=(2,))
def kinetic(params: Any, u: Any, rho: Callable) -> jax.Array:
    def sqrt_rho(params, u): return (rho(params, u)+1E-4)**0.5  # flax format
    lap_val = laplacian(params, u, sqrt_rho)
    # return -0.5*jnp.mean(lap_val)
    rho_val = rho(params, u)
    rho_val = 1./(rho_val+1E-4)**0.5  # for numerical stability
    return -0.5*jnp.multiply(rho_val, lap_val)


@partial(jit,  static_argnums=(2,))
def weizsacker(params: Any, u: Array, fun: callable, l: Any = .2) -> jax.Array:
    """
    l = 0.2 (W Stich, EKU Gross., Physik A Atoms and Nuclei, 309(1):511, 1982.)
    T_{\text{Weizsacker}}[\rho] &=& \frac{\lambda}{8} \int \frac{(\nabla \rho)^2}{\rho} dr = 
                            &=&    \frac{\lambda}{8} \int  \rho \left(\frac{(\nabla \rho)}{\rho}\right)^2 dr\\
    T_{\text{Weizsacker}}[\rho] = \mathbb{E}_{\rho} \left[ \left(\frac{(\nabla \rho)}{\rho}\right)^2 \right]

    Args:
        params (Any): _description_
        u (Array): _description_
        fun (callable): _description_
        l (Any, optional): _description_. Defaults to 1..

    Returns:
        jax.Array: _description_
    """
    score_ = score(params, u, fun)
    rho_ = fun(params, u)
    val = (score_/rho_)**2
    return (l/8.)*val


@partial(jit,  static_argnums=(2,))
def thomas_fermi(params: Any, u: Array, fun: callable) -> jax.Array:
    """_summary_

    T_{\text{TF}}[\rho] &=& \frac{3}{10}(3\pi^2)^{2/3} \int ( \rho)^{5/3} dr \\
    T_{\text{TF}}[\rho] = \mathbb{E}_{\rho} \left[ ( \rho)^{2/3} \right]

    Args:
        params (Any): _description_
        u (Array): _description_
        fun (callable): _description_

    Returns:
        jax.Array: _description_
    """
    rho_ = fun(params, u)
    val = (rho_)**(2/3)
    l = (3./10.)*(3.*jnp.pi**2)**(2/3)
    return l*val


@partial(jit,  static_argnums=(2,))
def thomas_fermi_1D(params: Any, u: Array, fun: callable) -> jax.Array:
    """_summary_

    T_{\text{TF}}[\rho] &=& \frac{\pi^2}{12}\int ( \rho)^{} dr \\
    T_{\text{TF}}[\rho] = \frac{\pi^2}{12}\mathbb{E}_{\rho} \left[ ( \rho)^{2} \right]

    Args:
        params (Any): _description_
        u (Array): _description_
        fun (callable): _description_

    Returns:
        jax.Array: _description_
    """
    rho_ = fun(params, u)
    val = rho_*rho_
    l = (jnp.pi*jnp.pi)/12.
    return l*val
# ------------------------------------------------------------------------------------------------------------


@partial(jit,  static_argnums=(2,))
def Dirac_exchange(params: Any, u: Array, fun: callable) -> jax.Array:
    """_summary_

    ^{Dirac}E_{\text{x}}[\rho] = -\frac{3}{4}\left(\frac{3}{\pi}\right)^{1/3}\int  \rho^{4/3} dr \\
    ^{Dirac}E_{\text{x}}[\rho] = -\frac{3}{4}\left(\frac{3}{\pi}\right)^{1/3}\mathbb{E}_{\rho} \left[ \rho^{1/3} \right]

    Args:
        params (Any): _description_
        u (Array): _description_
        fun (callable): _description_

    Returns:
        jax.Array: _description_
    """
    rho_ = fun(params, u)
    l = -(3/4)*(3/jnp.pi)**1/3
    return l*rho_**(1/3)

# ------------------------------------------------------------------------------------------------------------


@partial(jit,  static_argnums=(2, 3))
def GaussianPotential1D(params: Any, u: Any, T: Callable,  params_pot: Any = None) -> jax.Array:
    if (params_pot is None):
        params_pot = {'alpha': jnp.array([[1.], [2.]]),  # Ha/electron
                      'beta': -1.*jnp.array([[-0.5], [1.]])}  # BHOR

    # x = T(u)
    x = T(params, u)

    @jit
    def _f(x: Array, params_pot: Any):
        alpha, beta = params_pot['alpha'], params_pot['beta']
        return -alpha*jnp.exp(-(x-beta)*(x-beta))  # **2 OLD

    y = vmap(_f, in_axes=(None, 1))(x, params_pot)
    y = jnp.sum(y, axis=-1).transpose()
    return y


@partial(jit,  static_argnums=(2, 3))
def GaussianPotential1D_pot(params: Any, u: Any, T: Callable,  params_pot: Any = None) -> jax.Array:
    if (params_pot is None):
        params_pot = {'alpha': jnp.array([[1.], [2.]]),  # Ha/electron
                      'beta': -1.*jnp.array([[-0.5], [1.]])}  # BHOR

    # x = T(u)
    x = T(params, u)

    @jit
    def _f(x: Array, params_pot: Any):
        alpha, beta = params_pot['alpha'], params_pot['beta']
        return -alpha*jnp.exp(-(x-beta)*(x-beta))  # **2 OLD

    y = vmap(_f, in_axes=(None, 1))(x, params_pot)
    y = jnp.sum(y, axis=-1).transpose()
    return y


@partial(jax.jit,  static_argnums=(3,))
def Coulomb_potential(params: Any, u: Any, up: Any, T: Callable, eps=1E-3):
    x = T(params, u)
    xp = T(params, up)
    z = 1./jnp.linalg.norm(x-xp, axis=1)
    return 0.5*z


@partial(jax.jit,  static_argnums=(3, 4,))
def Hartree_potential(params: Any, u: Any, up: Any, T: Callable, eps=1E-3):
    x = T(params, u)
    xp = T(params, up)
    z = (x-xp)*(x-xp)
    z = 1./(z**0.5+eps)
    return 0.5*z


@partial(jax.jit,  static_argnums=(2,))
def Nuclei_potential(params: Any, u: Any, T: Callable, mol_info: Any):
    eps = 0.001  # 0.2162

    @jit
    def _potential(x: Any, molecule: Any):
        r = jnp.sqrt(
            jnp.sum((x-molecule['coords'])*(x-molecule['coords']), axis=1)) + eps
        z = molecule['z']
        return z/r

    x = T(params, u)
    r = vmap(_potential, in_axes=(None, 0), out_axes=1)(x, mol_info)
    r = jnp.sum(r, axis=1)
    return -lax.expand_dims(r, dimensions=(1,))


@partial(jax.jit,  static_argnums=(1,))
def cusp_condition(params: Any, fun: callable, mol_info: Any):

    @jax.jit
    def _cusp(molecule: Any):
        x = molecule['coords'][None]
        z = molecule['z']
        rho_val = fun(params, x)
        d_rho_val = score(params, x, fun)
        return jnp.sum(d_rho_val) - (-2*z*jnp.sum(rho_val))

    l = vmap(_cusp)(mol_info)
    return jnp.mean(jnp.abs(l))


if __name__ == '__main__':

    coords = jnp.array([[0., 0., -1.4008538753/2], [0., 0., 1.4008538753/2]])
    z = jnp.array([[1.], [1.]])
    # atoms = ['H', 'H']
    mol = {'coords': coords, 'z': z}  # 'atoms': atoms

    rng = jax.random.PRNGKey(0)
    _, key = jax.random.split(rng)
    x = jax.random.uniform(key, shape=(10, 3))

    y = Nuclei_potential(None, x, None, mol)
    print(x.shape)
    print(y.shape)
    print(x)
