import numpy as np
import warnings
import tensorly as tl
from .proximal import soft_thresholding
import scipy.sparse.linalg


def svd_flip(U, V, u_based_decision=True):
    """Sign correction to ensure deterministic output from SVD.
    Adjusts the columns of u and the rows of v such that the loadings in the
    columns in u that are largest in absolute value are always positive.
    This function is borrowed from scikit-learn/utils/extmath.py
    Parameters
    ----------
    U : ndarray
        u and v are the output of SVD
    V : ndarray
        u and v are the output of SVD
    u_based_decision : boolean, (default=True)
        If True, use the columns of u as the basis for sign flipping.
        Otherwise, use the rows of v. The choice of which variable to base the
        decision on is generally algorithm dependent.
    Returns
    -------
    u_adjusted, v_adjusted : arrays with the same dimensions as the input.
    """
    if u_based_decision:
        # columns of U, rows of V
        max_abs_cols = tl.argmax(tl.abs(U), axis=0)
        signs = tl.sign(
            tl.tensor(
                [U[i, j] for (i, j) in zip(max_abs_cols, range(tl.shape(U)[1]))],
                **tl.context(U),
            )
        )
        U = U * signs
        if tl.shape(V)[0] > tl.shape(U)[1]:
            signs = tl.concatenate((signs, tl.ones(tl.shape(V)[0] - tl.shape(U)[1])))
        V = V * signs[: tl.shape(V)[0]][:, None]
    else:
        # rows of V, columns of U
        max_abs_rows = tl.argmax(tl.abs(V), axis=1)
        signs = tl.sign(
            tl.tensor(
                [V[i, j] for (i, j) in zip(range(tl.shape(V)[0]), max_abs_rows)],
                **tl.context(V),
            )
        )
        V = V * signs[:, None]
        if tl.shape(U)[1] > tl.shape(V)[0]:
            signs = tl.concatenate((signs, tl.ones(tl.shape(U)[1] - tl.shape(V)[0])))
        U = U * signs[: tl.shape(U)[1]]

    return U, V


def make_svd_non_negative(tensor, U, S, V, nntype):
    """Use NNDSVD method to transform SVD results into a non-negative form. This
    method leads to more efficient solving with NNMF [1].

    Parameters
    ----------
    tensor : tensor being decomposed
    U, S, V: SVD factorization results
    nntype : {'nndsvd', 'nndsvda'}
        Whether to fill small values with 0.0 (nndsvd), or the tensor mean (nndsvda, default).

    [1]: Boutsidis & Gallopoulos. Pattern Recognition, 41(4): 1350-1362, 2008.
    """

    # NNDSVD initialization
    W = tl.zeros_like(U)
    H = tl.zeros_like(V)

    # The leading singular triplet is non-negative
    # so it can be used as is for initialization.
    W = tl.index_update(W, tl.index[:, 0], tl.sqrt(S[0]) * tl.abs(U[:, 0]))
    H = tl.index_update(H, tl.index[0, :], tl.sqrt(S[0]) * tl.abs(V[0, :]))

    for j in range(1, tl.shape(U)[1]):
        x, y = U[:, j], V[j, :]

        # extract positive and negative parts of column vectors
        x_p, y_p = tl.clip(x, a_min=0.0), tl.clip(y, a_min=0.0)
        x_n, y_n = tl.abs(tl.clip(x, a_max=0.0)), tl.abs(tl.clip(y, a_max=0.0))

        # and their norms
        x_p_nrm, y_p_nrm = tl.norm(x_p), tl.norm(y_p)
        x_n_nrm, y_n_nrm = tl.norm(x_n), tl.norm(y_n)

        m_p, m_n = x_p_nrm * y_p_nrm, x_n_nrm * y_n_nrm

        # choose update
        if m_p > m_n:
            u = x_p / x_p_nrm
            v = y_p / y_p_nrm
            sigma = m_p
        else:
            u = x_n / x_n_nrm
            v = y_n / y_n_nrm
            sigma = m_n

        lbd = tl.sqrt(S[j] * sigma)
        W = tl.index_update(W, tl.index[:, j], lbd * u)
        H = tl.index_update(H, tl.index[j, :], lbd * v)

    # After this point we no longer need H
    eps = tl.eps(tensor.dtype)

    if nntype == "nndsvd":
        W = soft_thresholding(W, eps)
    elif nntype == "nndsvda":
        avg = tl.mean(tensor)
        W = tl.where(W < eps, tl.ones(tl.shape(W), **tl.context(W)) * avg, W)
    else:
        raise ValueError(
            "Invalid nntype parameter: got %r instead of one of %r"
            % (nntype, ("nndsvd", "nndsvda"))
        )

    return W


def randomized_range_finder(A, n_dims, n_iter=2, random_state=None):
    """Computes an orthonormal matrix (Q) whose range approximates the range of A,  i.e., Q Q^H A ≈ A

    Parameters
    ----------
    A : 2D-array
    n_dims : int, dimension of the returned subspace
    n_iter : int, number of power iterations to conduct (default = 2)
    random_state: {None, int, np.random.RandomState}

    Returns
    -------
    Q : 2D-array
        of shape (A.shape[0], min(n_dims, A.shape[0], A.shape[1]))

    Notes
    -----
    This function is implemented based on Algorith 4.4 in `Finding structure with randomness:
    Probabilistic algorithms for constructing approximate matrix decompositions`
    - Halko et al (2009)
    """
    rng = tl.check_random_state(random_state)
    dim_1, dim_2 = tl.shape(A)
    Q = tl.tensor(rng.normal(size=(dim_2, n_dims)), **tl.context(A))
    Q, _ = tl.qr(tl.dot(A, Q))

    # Perform power iterations when spectrum decays slowly
    A_H = tl.conj(tl.transpose(A))
    for i in range(n_iter):
        Q, _ = tl.qr(tl.dot(A_H, Q))
        Q, _ = tl.qr(tl.dot(A, Q))

    return Q


def partial_svd(matrix, n_eigenvecs=None, random_state=None, **kwargs):
    """Computes a fast partial SVD on `matrix`

    If `n_eigenvecs` is specified, sparse eigendecomposition is used on
    either matrix.dot(matrix.T) or matrix.T.dot(matrix).

    Parameters
    ----------
    matrix : tensor
        A 2D tensor.
    n_eigenvecs : int, optional, default is None
        If specified, number of eigen[vectors-values] to return.
    flip : bool, default is False
        If True, the SVD sign ambiguity is resolved by making the largest component
        in the columns of U, positive.
    random_state: {None, int, np.random.RandomState}
        If specified, use it for sampling starting vector in a partial SVD(scipy.sparse.linalg.eigsh)
    **kwargs : optional
        kwargs are used to absorb the difference of parameters among the other SVD functions

    Returns
    -------
    U : 2-D tensor, shape (matrix.shape[0], n_eigenvecs)
        Contains the right singular vectors
    S : 1-D tensor, shape (n_eigenvecs, )
        Contains the singular values of `matrix`
    V : 2-D tensor, shape (n_eigenvecs, matrix.shape[1])
        Contains the left singular vectors
    """
    # Check that matrix is... a matrix!
    if tl.ndim(matrix) != 2:
        raise ValueError("matrix be a matrix. matrix.ndim is %d != 2" % tl.ndim(matrix))

    ctx = tl.context(matrix)
    is_numpy = isinstance(matrix, np.ndarray)
    if not is_numpy:
        warnings.warn(
            "In partial_svd: converting to NumPy."
            " Check svd_funs for available alternatives if you want to avoid this."
        )

    # Choose what to do depending on the params
    dim_1, dim_2 = tl.shape(matrix)
    min_dim = min(dim_1, dim_2)

    if (n_eigenvecs is None) or (n_eigenvecs >= min_dim):
        # Just perform trucated SVD
        full_matrices = (n_eigenvecs is None) or (n_eigenvecs > min_dim)
        # If n_eigenvecs == min_dim, we don't want full_matrices=True, it's super slow
        U, S, V = tl.svd(matrix, full_matrices=full_matrices)
        U, S, V = U[:, :n_eigenvecs], S[:n_eigenvecs], V[:n_eigenvecs, :]
    else:
        matrix = tl.to_numpy(matrix)
        # We can perform a partial SVD
        rng = tl.check_random_state(random_state)
        # initilize with [-1, 1] as in ARPACK
        v0 = rng.uniform(-1, 1, min_dim)

        # First choose whether to use X * X.T or X.T *X
        if dim_1 < dim_2:
            S, U = scipy.sparse.linalg.eigsh(
                np.dot(matrix, matrix.T.conj()), k=n_eigenvecs, which="LM", v0=v0
            )
            S = np.sqrt(np.clip(S, 0, None))
            S = np.clip(
                S, np.finfo(S.dtype).eps, None
            )  # To avoid divide by zero warning on next line
            V = np.dot(
                matrix.T.conj(),
                U * np.where(np.abs(S) <= np.finfo(S.dtype).eps, 0, 1 / S)[None, :],
            )
            U, S, V = U[:, ::-1], S[::-1], V[:, ::-1]
            V, R = np.linalg.qr(V)
            V = V * (
                2 * (np.diag(R) >= 0) - 1
            )  # we can't use np.sign because np.sign(0) == 0
        else:
            S, V = scipy.sparse.linalg.eigsh(
                np.dot(matrix.T.conj(), matrix), k=n_eigenvecs, which="LM", v0=v0
            )
            S = np.sqrt(np.clip(S, 0, None))
            S = np.clip(S, np.finfo(S.dtype).eps, None)
            U = (
                np.dot(matrix, V)
                * np.where(np.abs(S) <= np.finfo(S.dtype).eps, 0, 1 / S)[None, :]
            )
            U, S, V = U[:, ::-1], S[::-1], V[:, ::-1]
            U, R = np.linalg.qr(U)
            U = U * (2 * (np.diag(R) >= 0) - 1)

        if not is_numpy:
            U = tl.tensor(U, **ctx)
            S = tl.tensor(S, **ctx)
            V = tl.tensor(V, **ctx)

        # WARNING: here, V is still the transpose of what it should be
        V = V.T.conj()

    return U, S, V


def truncated_svd(matrix, n_eigenvecs=None, **kwargs):
    """Computes a truncated SVD on `matrix` using the backends's standard SVD

    Parameters
    ----------
    matrix : 2D-array
    n_eigenvecs : int, optional, default is None
        if specified, number of eigen[vectors-values] to return

    Returns
    -------
    U : 2D-array
        of shape (matrix.shape[0], n_eigenvecs)
        contains the right singular vectors
    S : 1D-array
        of shape (n_eigenvecs, )
        contains the singular values of `matrix`
    V : 2D-array
        of shape (n_eigenvecs, matrix.shape[1])
        contains the left singular vectors
    """
    # Check that matrix is... a matrix!
    if tl.ndim(matrix) != 2:
        raise ValueError("matrix be a matrix. matrix.ndim is %d != 2" % tl.ndim(matrix))

    dim_1, dim_2 = tl.shape(matrix)
    min_dim, max_dim = min(dim_1, dim_2), max(dim_1, dim_2)

    if n_eigenvecs is None:
        n_eigenvecs = max_dim

    if n_eigenvecs > max_dim:
        warnings.warn(
            "Trying to compute SVD with n_eigenvecs={0}, which "
            "is larger than max(matrix.shape)={1}. Setting "
            "n_eigenvecs to {1}".format(n_eigenvecs, max_dim)
        )
        n_eigenvecs = max_dim

    full_matrices = n_eigenvecs > min_dim

    U, S, V = tl.svd(matrix, full_matrices=full_matrices)
    U, S, V = U[:, :n_eigenvecs], S[:n_eigenvecs], V[:n_eigenvecs, :]
    return U, S, V


def symeig_svd(matrix, n_eigenvecs=None, **kwargs):
    """Computes a truncated SVD on `matrix` using symeig

        Uses symeig on matrix.T.dot(matrix) or its transpose

    Parameters
    ----------
    matrix : 2D-array
    n_eigenvecs : int, optional, default is None
        if specified, number of eigen[vectors-values] to return
    **kwargs : optional
        kwargs are used to absorb the difference of parameters among the other SVD functions

    Returns
    -------
    U : 2D-array
        of shape (matrix.shape[0], n_eigenvecs)
        contains the right singular vectors
    S : 1D-array
        of shape (n_eigenvecs, )
        contains the singular values of `matrix`
    V : 2D-array
        of shape (n_eigenvecs, matrix.shape[1])
        contains the left singular vectors
    """
    # Check that matrix is... a matrix!
    if tl.ndim(matrix) != 2:
        raise ValueError("matrix be a matrix. matrix.ndim is %d != 2" % tl.ndim(matrix))

    dim_1, dim_2 = tl.shape(matrix)
    max_dim = max(dim_1, dim_2)

    if n_eigenvecs is None:
        n_eigenvecs = max_dim

    if n_eigenvecs > max_dim:
        warnings.warn(
            "Trying to compute SVD with n_eigenvecs={0}, which "
            "is larger than max(matrix.shape)={1}. Setting "
            "n_eigenvecs to {1}".format(n_eigenvecs, max_dim)
        )
        n_eigenvecs = max_dim

    if dim_1 > dim_2:
        S, U = tl.eigh(tl.dot(matrix, tl.transpose(matrix)))
        S = tl.sqrt(tl.clip(S, tl.eps(S.dtype)))
        V = tl.dot(tl.transpose(matrix), U / tl.reshape(S, (1, -1)))
    else:
        S, V = tl.eigh(tl.dot(tl.transpose(matrix), matrix))
        S = tl.sqrt(tl.clip(S, tl.eps(S.dtype)))
        U = tl.dot(matrix, V) / tl.reshape(S, (1, -1))

    U, S, V = (
        tl.flip(U, axis=1),
        tl.flip(S),
        tl.flip(tl.transpose(V), axis=0),
    )
    return (
        U[:, : min(dim_1, n_eigenvecs)],
        S[: min(dim_1, dim_2, n_eigenvecs)],
        V[: min(dim_2, n_eigenvecs), :],
    )


def randomized_svd(
    matrix,
    n_eigenvecs=None,
    n_oversamples=5,
    n_iter=2,
    random_state=None,
    **kwargs,
):
    """Computes a truncated randomized SVD.

    If `n_eigenvecs` is specified, sparse eigendecomposition is used on
    either matrix.dot(matrix.T) or matrix.T.dot(matrix).

    Parameters
    ----------
    matrix : tensor
        A 2D tensor.
    n_eigenvecs : int, optional, default is None
        If specified, number of eigen[vectors-values] to return.
    n_oversamples: int, optional, default = 5
        rank overestimation value for finiding subspace with better allignment
    n_iter: int, optional, default = 2
        number of power iterations for the `randomized_range_finder` subroutine
    random_state: {None, int, np.random.RandomState}
    **kwargs : optional
        kwargs are used to absorb the difference of parameters among the other SVD functions

    Returns
    -------
    U : 2-D tensor, shape (matrix.shape[0], n_eigenvecs)
        Contains the right singular vectors
    S : 1-D tensor, shape (n_eigenvecs, )
        Contains the singular values of `matrix`
    V : 2-D tensor, shape (n_eigenvecs, matrix.shape[1])
        Contains the left singular vectors

    Notes
    -----
    This function is implemented based on Algorith 5.1 in `Finding structure with randomness:
    Probabilistic algorithms for constructing approximate matrix decompositions`
    - Halko et al (2009)
    """
    # Check that matrix is... a matrix!
    if tl.ndim(matrix) != 2:
        raise ValueError(f"matrix be a matrix. matrix.ndim is {tl.ndim(matrix)} != 2")

    dim_1, dim_2 = tl.shape(matrix)
    min_dim, max_dim = min(dim_1, dim_2), max(dim_1, dim_2)

    if n_eigenvecs is None:
        n_eigenvecs = max_dim

    if n_eigenvecs > max_dim:
        warnings.warn(
            f"Trying to compute SVD with n_eigenvecs={n_eigenvecs}, which "
            f"is larger than max(matrix.shape)={max_dim}. Setting "
            f"n_eigenvecs to {max_dim}"
        )
        n_eigenvecs = max_dim

    n_dims = min(n_eigenvecs + n_oversamples, max_dim)

    if (
        dim_1 > dim_2
        and n_eigenvecs > min(min_dim, n_dims)
        or dim_1 < dim_2
        and n_eigenvecs < min(min_dim, n_dims)
    ):
        # transpose matrix to keep the reduced matrix shape minimal
        matrix_T = tl.transpose(matrix)
        Q = randomized_range_finder(
            matrix_T, n_dims=n_dims, n_iter=n_iter, random_state=random_state
        )
        Q_H = tl.conj(tl.transpose(Q))
        matrix_reduced = tl.transpose(tl.dot(Q_H, matrix_T))
        U, S, V = truncated_svd(matrix_reduced, n_eigenvecs=n_eigenvecs)
        V = tl.dot(V, tl.transpose(Q))
    else:
        Q = randomized_range_finder(
            matrix, n_dims=n_dims, n_iter=n_iter, random_state=random_state
        )
        Q_H = tl.conj(tl.transpose(Q))
        matrix_reduced = tl.dot(Q_H, matrix)
        U, S, V = truncated_svd(matrix_reduced, n_eigenvecs=n_eigenvecs)
        U = tl.dot(Q, U)

    return U, S, V


SVD_FUNS = ["partial_svd", "truncated_svd", "symeig_svd", "randomized_svd"]


def svd_funs(
    tensor,
    svd_type="partial_svd",
    n_eigenvecs=None,
    flip_svd=True,
    non_negative=False,
    nntype="nndsvd",
    u_based_decision=True,
    **kwargs,
):

    if svd_type == "partial_svd":
        U, S, V = partial_svd(tensor, n_eigenvecs=n_eigenvecs, **kwargs)
    elif svd_type == "truncated_svd":
        U, S, V = truncated_svd(tensor, n_eigenvecs=n_eigenvecs, **kwargs)
    elif svd_type == "symeig_svd":
        U, S, V = symeig_svd(tensor, n_eigenvecs=n_eigenvecs, **kwargs)
    elif svd_type == "randomized_svd":
        U, S, V = randomized_svd(tensor, n_eigenvecs=n_eigenvecs, **kwargs)
    else:
        raise ValueError(
            f"Got svd={svd_type}. However, the possible choices are {SVD_FUNS}"
        )

    if flip_svd:
        U, V = svd_flip(U, V, u_based_decision=u_based_decision)

    if non_negative:
        U = make_svd_non_negative(tensor, U, S, V, nntype)

    return U, S, V
