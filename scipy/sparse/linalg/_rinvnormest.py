from scipy.sparse.linalg import splu
from scipy.sparse.linalg._onenormest import *

__all__ = ['rinvnormest', 'cond1est']


def rinvnormest(A, norm="1"):
    """
    Compute an estimate for the reciprocal of the norm of the inverse 
    of a sparse matrix.

    Parameters
    ----------
    A : ndarray or other linear operator
        A sparse matrix for which an LU matrix can be computed. CSC would
        be most efficient.
    norm : string, optional
        "1"/"0" [default] computes the 1-norm, "I" computes the inf-norm.

    Returns
    -------
    est : float
        An estimate of the norm of the inverse matrix.

    Notes
    -----
    Computes an LU decomposition and runs the gscon procedure from SuperLU.
    Use scipy.sparse.linalg.SuperLU.rinvnormest if you already have an LU 
    decomposition.

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse import csc_matrix
    >>> from scipy.sparse.linalg import rinvnormest
    >>> A = csc_matrix([[1., 0., 0.], [5., 8., 2.], [0., -1., 0.]], dtype=float)
    >>> A.toarray()
    array([[ 1.,  0.,  0.],
           [ 5.,  8.,  2.],
           [ 0., -1.,  0.]])
    >>> rinvnormest(A,norm="1")
    0.2
    >>> 1/np.linalg.norm(np.linalg.inv(A.toarray()), ord=1)
    0.2
    """
    lu_decomposition = splu(A)
    return lu_decomposition.rinvnormest(norm=norm)

def cond1est(A):
    """
    Compute an estimate for the reciprocal of the condition number 
    of a sparse matrix, using 1-norms.

    Parameters
    ----------
    A : ndarray or other linear operator
        A sparse matrix for which an LU matrix can be computed. CSC would
        be most efficient.

    Returns
    -------
    cond : float
        An estimate of the 1-norm condition number of A.

    Notes
    -----
    Computes an LU decomposition and runs the gscon procedure from SuperLU.
    Use scipy.sparse.linalg.SuperLU.rinvnormest if you already have an LU 
    decomposition.

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.sparse import csc_matrix
    >>> from scipy.sparse.linalg import cond1est
    >>> A = csc_matrix([[1., 0., 0.], [5., 8., 2.], [0., -1., 0.]], dtype=float)
    >>> A.toarray()
    array([[ 1.,  0.,  0.],
           [ 5.,  8.,  2.],
           [ 0., -1.,  0.]])
    >>> cond1est(A)
    45.0
    >>> np.linalg.cond(A.toarray(), p=1)
    45.0
    """
    return onenormest(A)/rinvnormest(A)