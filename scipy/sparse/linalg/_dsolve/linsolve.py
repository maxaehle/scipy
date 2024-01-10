from warnings import warn

import numpy as np
from numpy import asarray
from scipy.sparse import (issparse,
                          SparseEfficiencyWarning, csc_matrix, eye, diags)
from scipy.sparse._sputils import is_pydata_spmatrix
from scipy.linalg import LinAlgError
import copy

from . import _superlu

noScikit = False
try:
    import scikits.umfpack as umfpack
except ImportError:
    noScikit = True

useUmfpack = not noScikit

__all__ = ['use_solver', 'spsolve', 'splu', 'spilu', 'factorized',
           'MatrixRankWarning', 'spsolve_triangular']


class MatrixRankWarning(UserWarning):
    pass


def use_solver(**kwargs):
    """
    Select default sparse direct solver to be used.

    Parameters
    ----------
    useUmfpack : bool, optional
        Use UMFPACK [1]_, [2]_, [3]_, [4]_. over SuperLU. Has effect only
        if ``scikits.umfpack`` is installed. Default: True
    assumeSortedIndices : bool, optional
        Allow UMFPACK to skip the step of sorting indices for a CSR/CSC matrix.
        Has effect only if useUmfpack is True and ``scikits.umfpack`` is
        installed. Default: False

    Notes
    -----
    The default sparse solver is UMFPACK when available
    (``scikits.umfpack`` is installed). This can be changed by passing
    useUmfpack = False, which then causes the always present SuperLU
    based solver to be used.

    UMFPACK requires a CSR/CSC matrix to have sorted column/row indices. If
    sure that the matrix fulfills this, pass ``assumeSortedIndices=True``
    to gain some speed.

    References
    ----------
    .. [1] T. A. Davis, Algorithm 832:  UMFPACK - an unsymmetric-pattern
           multifrontal method with a column pre-ordering strategy, ACM
           Trans. on Mathematical Software, 30(2), 2004, pp. 196--199.
           https://dl.acm.org/doi/abs/10.1145/992200.992206

    .. [2] T. A. Davis, A column pre-ordering strategy for the
           unsymmetric-pattern multifrontal method, ACM Trans.
           on Mathematical Software, 30(2), 2004, pp. 165--195.
           https://dl.acm.org/doi/abs/10.1145/992200.992205

    .. [3] T. A. Davis and I. S. Duff, A combined unifrontal/multifrontal
           method for unsymmetric sparse matrices, ACM Trans. on
           Mathematical Software, 25(1), 1999, pp. 1--19.
           https://doi.org/10.1145/305658.287640

    .. [4] T. A. Davis and I. S. Duff, An unsymmetric-pattern multifrontal
           method for sparse LU factorization, SIAM J. Matrix Analysis and
           Computations, 18(1), 1997, pp. 140--158.
           https://doi.org/10.1137/S0895479894246905T.

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse.linalg import use_solver, spsolve
    >>> from scipy.sparse import csc_matrix
    >>> R = np.random.randn(5, 5)
    >>> A = csc_matrix(R)
    >>> b = np.random.randn(5)
    >>> use_solver(useUmfpack=False) # enforce superLU over UMFPACK
    >>> x = spsolve(A, b)
    >>> np.allclose(A.dot(x), b)
    True
    >>> use_solver(useUmfpack=True) # reset umfPack usage to default
    """
    if 'useUmfpack' in kwargs:
        globals()['useUmfpack'] = kwargs['useUmfpack']
    if useUmfpack and 'assumeSortedIndices' in kwargs:
        umfpack.configure(assumeSortedIndices=kwargs['assumeSortedIndices'])

def _get_umf_family(A):
    """Get umfpack family string given the sparse matrix dtype."""
    _families = {
        (np.float64, np.int32): 'di',
        (np.complex128, np.int32): 'zi',
        (np.float64, np.int64): 'dl',
        (np.complex128, np.int64): 'zl'
    }

    # A.dtype.name can only be "float64" or
    # "complex128" in control flow
    f_type = getattr(np, A.dtype.name)
    # control flow may allow for more index
    # types to get through here
    i_type = getattr(np, A.indices.dtype.name)

    try:
        family = _families[(f_type, i_type)]

    except KeyError as e:
        msg = ('only float64 or complex128 matrices with int32 or int64 '
               f'indices are supported! (got: matrix: {f_type}, indices: {i_type})')
        raise ValueError(msg) from e

    # See gh-8278. Considered converting only if
    # A.shape[0]*A.shape[1] > np.iinfo(np.int32).max,
    # but that didn't always fix the issue.
    family = family[0] + "l"
    A_new = copy.copy(A)
    A_new.indptr = np.array(A.indptr, copy=False, dtype=np.int64)
    A_new.indices = np.array(A.indices, copy=False, dtype=np.int64)

    return family, A_new

def _safe_downcast_indices(A):
    # check for safe downcasting
    max_value = np.iinfo(np.intc).max

    if A.indptr[-1] > max_value:  # indptr[-1] is max b/c indptr always sorted
        raise ValueError("indptr values too large for SuperLU")

    if max(*A.shape) > max_value:  # only check large enough arrays
        if np.any(A.indices > max_value):
            raise ValueError("indices values too large for SuperLU")

    indices = A.indices.astype(np.intc, copy=False)
    indptr = A.indptr.astype(np.intc, copy=False)
    return indices, indptr

def spsolve(A, b, permc_spec=None, use_umfpack=True):
    """Solve the sparse linear system Ax=b, where b may be a vector or a matrix.

    Parameters
    ----------
    A : ndarray or sparse matrix
        The square matrix A will be converted into CSC or CSR form
    b : ndarray or sparse matrix
        The matrix or vector representing the right hand side of the equation.
        If a vector, b.shape must be (n,) or (n, 1).
    permc_spec : str, optional
        How to permute the columns of the matrix for sparsity preservation.
        (default: 'COLAMD')

        - ``NATURAL``: natural ordering.
        - ``MMD_ATA``: minimum degree ordering on the structure of A^T A.
        - ``MMD_AT_PLUS_A``: minimum degree ordering on the structure of A^T+A.
        - ``COLAMD``: approximate minimum degree column ordering [1]_, [2]_.

    use_umfpack : bool, optional
        if True (default) then use UMFPACK for the solution [3]_, [4]_, [5]_,
        [6]_ . This is only referenced if b is a vector and
        ``scikits.umfpack`` is installed.

    Returns
    -------
    x : ndarray or sparse matrix
        the solution of the sparse linear equation.
        If b is a vector, then x is a vector of size A.shape[1]
        If b is a matrix, then x is a matrix of size (A.shape[1], b.shape[1])

    Notes
    -----
    For solving the matrix expression AX = B, this solver assumes the resulting
    matrix X is sparse, as is often the case for very sparse inputs.  If the
    resulting X is dense, the construction of this sparse result will be
    relatively expensive.  In that case, consider converting A to a dense
    matrix and using scipy.linalg.solve or its variants.

    References
    ----------
    .. [1] T. A. Davis, J. R. Gilbert, S. Larimore, E. Ng, Algorithm 836:
           COLAMD, an approximate column minimum degree ordering algorithm,
           ACM Trans. on Mathematical Software, 30(3), 2004, pp. 377--380.
           :doi:`10.1145/1024074.1024080`

    .. [2] T. A. Davis, J. R. Gilbert, S. Larimore, E. Ng, A column approximate
           minimum degree ordering algorithm, ACM Trans. on Mathematical
           Software, 30(3), 2004, pp. 353--376. :doi:`10.1145/1024074.1024079`

    .. [3] T. A. Davis, Algorithm 832:  UMFPACK - an unsymmetric-pattern
           multifrontal method with a column pre-ordering strategy, ACM
           Trans. on Mathematical Software, 30(2), 2004, pp. 196--199.
           https://dl.acm.org/doi/abs/10.1145/992200.992206

    .. [4] T. A. Davis, A column pre-ordering strategy for the
           unsymmetric-pattern multifrontal method, ACM Trans.
           on Mathematical Software, 30(2), 2004, pp. 165--195.
           https://dl.acm.org/doi/abs/10.1145/992200.992205

    .. [5] T. A. Davis and I. S. Duff, A combined unifrontal/multifrontal
           method for unsymmetric sparse matrices, ACM Trans. on
           Mathematical Software, 25(1), 1999, pp. 1--19.
           https://doi.org/10.1145/305658.287640

    .. [6] T. A. Davis and I. S. Duff, An unsymmetric-pattern multifrontal
           method for sparse LU factorization, SIAM J. Matrix Analysis and
           Computations, 18(1), 1997, pp. 140--158.
           https://doi.org/10.1137/S0895479894246905T.


    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse import csc_matrix
    >>> from scipy.sparse.linalg import spsolve
    >>> A = csc_matrix([[3, 2, 0], [1, -1, 0], [0, 5, 1]], dtype=float)
    >>> B = csc_matrix([[2, 0], [-1, 0], [2, 0]], dtype=float)
    >>> x = spsolve(A, B)
    >>> np.allclose(A.dot(x).toarray(), B.toarray())
    True
    """

    if is_pydata_spmatrix(A):
        A = A.to_scipy_sparse().tocsc()

    if not (issparse(A) and A.format in ("csc", "csr")):
        A = csc_matrix(A)
        warn('spsolve requires A be CSC or CSR matrix format',
             SparseEfficiencyWarning, stacklevel=2)

    # b is a vector only if b have shape (n,) or (n, 1)
    b_is_sparse = issparse(b) or is_pydata_spmatrix(b)
    if not b_is_sparse:
        b = asarray(b)
    b_is_vector = ((b.ndim == 1) or (b.ndim == 2 and b.shape[1] == 1))

    # sum duplicates for non-canonical format
    A.sum_duplicates()
    A = A._asfptype()  # upcast to a floating point format
    result_dtype = np.promote_types(A.dtype, b.dtype)
    if A.dtype != result_dtype:
        A = A.astype(result_dtype)
    if b.dtype != result_dtype:
        b = b.astype(result_dtype)

    # validate input shapes
    M, N = A.shape
    if (M != N):
        raise ValueError(f"matrix must be square (has shape {(M, N)})")

    if M != b.shape[0]:
        raise ValueError(f"matrix - rhs dimension mismatch ({A.shape} - {b.shape[0]})")

    use_umfpack = use_umfpack and useUmfpack

    if b_is_vector and use_umfpack:
        if b_is_sparse:
            b_vec = b.toarray()
        else:
            b_vec = b
        b_vec = asarray(b_vec, dtype=A.dtype).ravel()

        if noScikit:
            raise RuntimeError('Scikits.umfpack not installed.')

        if A.dtype.char not in 'dD':
            raise ValueError("convert matrix data to double, please, using"
                  " .astype(), or set linsolve.useUmfpack = False")

        umf_family, A = _get_umf_family(A)
        umf = umfpack.UmfpackContext(umf_family)
        x = umf.linsolve(umfpack.UMFPACK_A, A, b_vec,
                         autoTranspose=True)
    else:
        if b_is_vector and b_is_sparse:
            b = b.toarray()
            b_is_sparse = False

        if not b_is_sparse:
            if A.format == "csc":
                flag = 1  # CSC format
            else:
                flag = 0  # CSR format

            indices = A.indices.astype(np.intc, copy=False)
            indptr = A.indptr.astype(np.intc, copy=False)
            options = dict(ColPerm=permc_spec)
            x, info = _superlu.gssv(N, A.nnz, A.data, indices, indptr,
                                    b, flag, options=options)
            if info != 0:
                warn("Matrix is exactly singular", MatrixRankWarning, stacklevel=2)
                x.fill(np.nan)
            if b_is_vector:
                x = x.ravel()
        else:
            # b is sparse
            Afactsolve = factorized(A)

            if not (b.format == "csc" or is_pydata_spmatrix(b)):
                warn('spsolve is more efficient when sparse b '
                     'is in the CSC matrix format',
                     SparseEfficiencyWarning, stacklevel=2)
                b = csc_matrix(b)

            # Create a sparse output matrix by repeatedly applying
            # the sparse factorization to solve columns of b.
            data_segs = []
            row_segs = []
            col_segs = []
            for j in range(b.shape[1]):
                # TODO: replace this with
                # bj = b[:, j].toarray().ravel()
                # once 1D sparse arrays are supported.
                # That is a slightly faster code path.
                bj = b[:, [j]].toarray().ravel()
                xj = Afactsolve(bj)
                w = np.flatnonzero(xj)
                segment_length = w.shape[0]
                row_segs.append(w)
                col_segs.append(np.full(segment_length, j, dtype=int))
                data_segs.append(np.asarray(xj[w], dtype=A.dtype))
            sparse_data = np.concatenate(data_segs)
            sparse_row = np.concatenate(row_segs)
            sparse_col = np.concatenate(col_segs)
            x = A.__class__((sparse_data, (sparse_row, sparse_col)),
                           shape=b.shape, dtype=A.dtype)

            if is_pydata_spmatrix(b):
                x = b.__class__(x)

    return x


def splu(A, permc_spec=None, diag_pivot_thresh=None,
         relax=None, panel_size=None, options=dict()):
    """
    Compute the LU decomposition of a sparse, square matrix.

    Parameters
    ----------
    A : sparse matrix
        Sparse matrix to factorize. Most efficient when provided in CSC
        format. Other formats will be converted to CSC before factorization.
    permc_spec : str, optional
        How to permute the columns of the matrix for sparsity preservation.
        (default: 'COLAMD')

        - ``NATURAL``: natural ordering.
        - ``MMD_ATA``: minimum degree ordering on the structure of A^T A.
        - ``MMD_AT_PLUS_A``: minimum degree ordering on the structure of A^T+A.
        - ``COLAMD``: approximate minimum degree column ordering

    diag_pivot_thresh : float, optional
        Threshold used for a diagonal entry to be an acceptable pivot.
        See SuperLU user's guide for details [1]_
    relax : int, optional
        Expert option for customizing the degree of relaxing supernodes.
        See SuperLU user's guide for details [1]_
    panel_size : int, optional
        Expert option for customizing the panel size.
        See SuperLU user's guide for details [1]_
    options : dict, optional
        Dictionary containing additional expert options to SuperLU.
        See SuperLU user guide [1]_ (section 2.4 on the 'Options' argument)
        for more details. For example, you can specify
        ``options=dict(Equil=False, IterRefine='SINGLE'))``
        to turn equilibration off and perform a single iterative refinement.

    Returns
    -------
    invA : scipy.sparse.linalg.SuperLU
        Object, which has a ``solve`` method.

    See also
    --------
    spilu : incomplete LU decomposition

    Notes
    -----
    This function uses the SuperLU library.

    References
    ----------
    .. [1] SuperLU https://portal.nersc.gov/project/sparse/superlu/

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse import csc_matrix
    >>> from scipy.sparse.linalg import splu
    >>> A = csc_matrix([[1., 0., 0.], [5., 0., 2.], [0., -1., 0.]], dtype=float)
    >>> B = splu(A)
    >>> x = np.array([1., 2., 3.], dtype=float)
    >>> B.solve(x)
    array([ 1. , -3. , -1.5])
    >>> A.dot(B.solve(x))
    array([ 1.,  2.,  3.])
    >>> B.solve(A.dot(x))
    array([ 1.,  2.,  3.])
    """

    if is_pydata_spmatrix(A):
        def csc_construct_func(*a, cls=type(A)):
            return cls(csc_matrix(*a))
        A = A.to_scipy_sparse().tocsc()
    else:
        csc_construct_func = csc_matrix

    if not (issparse(A) and A.format == "csc"):
        A = csc_matrix(A)
        warn('splu converted its input to CSC format',
             SparseEfficiencyWarning, stacklevel=2)

    # sum duplicates for non-canonical format
    A.sum_duplicates()
    A = A._asfptype()  # upcast to a floating point format

    M, N = A.shape
    if (M != N):
        raise ValueError("can only factor square matrices")  # is this true?

    indices, indptr = _safe_downcast_indices(A)

    _options = dict(DiagPivotThresh=diag_pivot_thresh, ColPerm=permc_spec,
                    PanelSize=panel_size, Relax=relax)
    if options is not None:
        _options.update(options)

    # Ensure that no column permutations are applied
    if (_options["ColPerm"] == "NATURAL"):
        _options["SymmetricMode"] = True

    return _superlu.gstrf(N, A.nnz, A.data, indices, indptr,
                          csc_construct_func=csc_construct_func,
                          ilu=False, options=_options)


def spilu(A, drop_tol=None, fill_factor=None, drop_rule=None, permc_spec=None,
          diag_pivot_thresh=None, relax=None, panel_size=None, options=None):
    """
    Compute an incomplete LU decomposition for a sparse, square matrix.

    The resulting object is an approximation to the inverse of `A`.

    Parameters
    ----------
    A : (N, N) array_like
        Sparse matrix to factorize. Most efficient when provided in CSC format.
        Other formats will be converted to CSC before factorization.
    drop_tol : float, optional
        Drop tolerance (0 <= tol <= 1) for an incomplete LU decomposition.
        (default: 1e-4)
    fill_factor : float, optional
        Specifies the fill ratio upper bound (>= 1.0) for ILU. (default: 10)
    drop_rule : str, optional
        Comma-separated string of drop rules to use.
        Available rules: ``basic``, ``prows``, ``column``, ``area``,
        ``secondary``, ``dynamic``, ``interp``. (Default: ``basic,area``)

        See SuperLU documentation for details.

    Remaining other options
        Same as for `splu`

    Returns
    -------
    invA_approx : scipy.sparse.linalg.SuperLU
        Object, which has a ``solve`` method.

    See also
    --------
    splu : complete LU decomposition

    Notes
    -----
    To improve the better approximation to the inverse, you may need to
    increase `fill_factor` AND decrease `drop_tol`.

    This function uses the SuperLU library.

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse import csc_matrix
    >>> from scipy.sparse.linalg import spilu
    >>> A = csc_matrix([[1., 0., 0.], [5., 0., 2.], [0., -1., 0.]], dtype=float)
    >>> B = spilu(A)
    >>> x = np.array([1., 2., 3.], dtype=float)
    >>> B.solve(x)
    array([ 1. , -3. , -1.5])
    >>> A.dot(B.solve(x))
    array([ 1.,  2.,  3.])
    >>> B.solve(A.dot(x))
    array([ 1.,  2.,  3.])
    """

    if is_pydata_spmatrix(A):
        def csc_construct_func(*a, cls=type(A)):
            return cls(csc_matrix(*a))
        A = A.to_scipy_sparse().tocsc()
    else:
        csc_construct_func = csc_matrix

    if not (issparse(A) and A.format == "csc"):
        A = csc_matrix(A)
        warn('spilu converted its input to CSC format',
             SparseEfficiencyWarning, stacklevel=2)

    # sum duplicates for non-canonical format
    A.sum_duplicates()
    A = A._asfptype()  # upcast to a floating point format

    M, N = A.shape
    if (M != N):
        raise ValueError("can only factor square matrices")  # is this true?

    indices, indptr = _safe_downcast_indices(A)

    _options = dict(ILU_DropRule=drop_rule, ILU_DropTol=drop_tol,
                    ILU_FillFactor=fill_factor,
                    DiagPivotThresh=diag_pivot_thresh, ColPerm=permc_spec,
                    PanelSize=panel_size, Relax=relax)
    if options is not None:
        _options.update(options)

    # Ensure that no column permutations are applied
    if (_options["ColPerm"] == "NATURAL"):
        _options["SymmetricMode"] = True

    return _superlu.gstrf(N, A.nnz, A.data, indices, indptr,
                          csc_construct_func=csc_construct_func,
                          ilu=True, options=_options)


def factorized(A):
    """
    Return a function for solving a sparse linear system, with A pre-factorized.

    Parameters
    ----------
    A : (N, N) array_like
        Input. A in CSC format is most efficient. A CSR format matrix will
        be converted to CSC before factorization.

    Returns
    -------
    solve : callable
        To solve the linear system of equations given in `A`, the `solve`
        callable should be passed an ndarray of shape (N,).

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse.linalg import factorized
    >>> from scipy.sparse import csc_matrix
    >>> A = np.array([[ 3. ,  2. , -1. ],
    ...               [ 2. , -2. ,  4. ],
    ...               [-1. ,  0.5, -1. ]])
    >>> solve = factorized(csc_matrix(A)) # Makes LU decomposition.
    >>> rhs1 = np.array([1, -2, 0])
    >>> solve(rhs1) # Uses the LU factors.
    array([ 1., -2., -2.])

    """
    if is_pydata_spmatrix(A):
        A = A.to_scipy_sparse().tocsc()

    if useUmfpack:
        if noScikit:
            raise RuntimeError('Scikits.umfpack not installed.')

        if not (issparse(A) and A.format == "csc"):
            A = csc_matrix(A)
            warn('splu converted its input to CSC format',
                 SparseEfficiencyWarning, stacklevel=2)

        A = A._asfptype()  # upcast to a floating point format

        if A.dtype.char not in 'dD':
            raise ValueError("convert matrix data to double, please, using"
                  " .astype(), or set linsolve.useUmfpack = False")

        umf_family, A = _get_umf_family(A)
        umf = umfpack.UmfpackContext(umf_family)

        # Make LU decomposition.
        umf.numeric(A)

        def solve(b):
            with np.errstate(divide="ignore", invalid="ignore"):
                # Ignoring warnings with numpy >= 1.23.0, see gh-16523
                result = umf.solve(umfpack.UMFPACK_A, A, b, autoTranspose=True)

            return result

        return solve
    else:
        return splu(A).solve


def spsolve_triangular(A, b, lower=True, overwrite_A=False, overwrite_b=False,
                       unit_diagonal=False):
    """
    Solve the equation ``A x = b`` for `x`, assuming A is a triangular matrix.

    Parameters
    ----------
    A : (M, M) sparse matrix
        A sparse square triangular matrix. Should be in CSR format.
    b : (M,) or (M, N) array_like
        Right-hand side matrix in ``A x = b``
    lower : bool, optional
        Whether `A` is a lower or upper triangular matrix.
        Default is lower triangular matrix.
    overwrite_A : bool, optional
        Allow changing `A`.
        Enabling gives a performance gain. Default is False.
    overwrite_b : bool, optional
        Allow overwriting data in `b`.
        Enabling gives a performance gain. Default is False.
        If `overwrite_b` is True, it should be ensured that
        `b` has an appropriate dtype to be able to store the result.
    unit_diagonal : bool, optional
        If True, diagonal elements of `a` are assumed to be 1.

        .. versionadded:: 1.4.0

    Returns
    -------
    x : (M,) or (M, N) ndarray
        Solution to the system ``A x = b``. Shape of return matches shape
        of `b`.

    Raises
    ------
    LinAlgError
        If `A` is singular or not triangular.
    ValueError
        If shape of `A` or shape of `b` do not match the requirements.

    Notes
    -----
    .. versionadded:: 0.19.0

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse import csc_array
    >>> from scipy.sparse.linalg import spsolve_triangular
    >>> A = csc_array([[3, 0, 0], [1, -1, 0], [2, 0, 1]], dtype=float)
    >>> B = np.array([[2, 0], [-1, 0], [2, 0]], dtype=float)
    >>> x = spsolve_triangular(A, B)
    >>> np.allclose(A.dot(x), B)
    True
    """

    if is_pydata_spmatrix(A):
        A = A.to_scipy_sparse().tocsc()
    
    trans = "N"
    if issparse(A) and A.format == "csr":
        A = A.T
        trans = "T"
        lower = not lower

    if not (issparse(A) and A.format == "csc"): 
        warn('CSC or CSR matrix format is required. Converting to CSC matrix.',
             SparseEfficiencyWarning, stacklevel=2)
        A = csc_matrix(A)
    elif not overwrite_A:
        A = A.copy()


    M, N = A.shape
    if M != N:
        raise ValueError(
            f'A must be a square matrix but its shape is {A.shape}.')

    if unit_diagonal:
        A = A.tolil()
        A.setdiag(1)
        A = A.tocsc()
    else:
        diag = A.diagonal()
        if np.any(diag == 0):
            raise LinAlgError(
                'A is singular: zero entry on diagonal.')
        invdiag = 1/diag
        if trans == "N":
            A = A @ diags(invdiag)
        else:
            A = (A.T @ diags(invdiag)).T

    # sum duplicates for non-canonical format
    A.sum_duplicates()

    b = np.asanyarray(b)

    if b.ndim not in [1, 2]:
        raise ValueError(
            f'b must have 1 or 2 dims but its shape is {b.shape}.')
    if M != b.shape[0]:
        raise ValueError(
            'The size of the dimensions of A must be equal to '
            'the size of the first dimension of b but the shape of A is '
            f'{A.shape} and the shape of b is {b.shape}.'
        )

    A = A._asfptype()  # upcast to a floating point format
    result_dtype = np.promote_types(A.dtype, b.dtype)
    if A.dtype != result_dtype:
        A = A.astype(result_dtype)
    if b.dtype != result_dtype:
        b = b.astype(result_dtype)
    if not overwrite_b:
        b = b.copy()

    if lower:
        L = A
        U = csc_matrix((N, N), dtype=result_dtype)
    else:
        L = eye(N, dtype=result_dtype, format='csc')
        U = A
        U.setdiag(0)

    x, info = _superlu.gstrs(trans,
                             N, L.nnz, L.data, L.indices, L.indptr,
                             N, U.nnz, U.data, U.indices, U.indptr,
                             b)
    if info:
        raise LinAlgError('A is singular.')

    if not unit_diagonal:
        invdiag = invdiag.reshape(-1, *([1] * (len(x.shape) - 1)))
        x = x * invdiag

    return x

