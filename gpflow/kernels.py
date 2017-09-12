# Copyright 2016 James Hensman, Valentine Svensson, alexggmatthews
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import print_function, absolute_import
from functools import reduce
import itertools
import warnings

import tensorflow as tf
import numpy as np
from .param import Param, Parameterized, AutoFlow
from . import transforms
from ._settings import settings
from .quadrature import hermgauss, mvhermgauss, mvnquad

float_type = settings.dtypes.float_type
int_type = settings.dtypes.int_type
np_float_type = np.float32 if float_type is tf.float32 else np.float64


class Kern(Parameterized):
    """
    The basic kernel class. Handles input_dim and active dims, and provides a
    generic '_slice' function to implement them.
    """

    def __init__(self, input_dim, active_dims=None):
        """
        input dim is an integer
        active dims is either an iterable of integers or None.

        Input dim is the number of input dimensions to the kernel. If the
        kernel is computed on a matrix X which has more columns than input_dim,
        then by default, only the first input_dim columns are used. If
        different columns are required, then they may be specified by
        active_dims.

        If active dims is None, it effectively defaults to range(input_dim),
        but we store it as a slice for efficiency.
        """
        Parameterized.__init__(self)
        self.scoped_keys.extend(['K', 'Kdiag'])
        self.input_dim = int(input_dim)
        if active_dims is None:
            self.active_dims = slice(input_dim)
        elif type(active_dims) is slice:
            self.active_dims = active_dims
            if active_dims.start is not None and active_dims.stop is not None and active_dims.step is not None:
                assert len(range(*active_dims)) == input_dim  # pragma: no cover
        else:
            self.active_dims = np.array(active_dims, dtype=np.int32)
            assert len(active_dims) == input_dim

        self.num_gauss_hermite_points = 20

    def _slice(self, X, X2):
        """
        Slice the correct dimensions for use in the kernel, as indicated by
        `self.active_dims`.
        :param X: Input 1 (NxD).
        :param X2: Input 2 (MxD), may be None.
        :return: Sliced X, X2, (Nxself.input_dim).
        """
        if isinstance(self.active_dims, slice):
            X = X[:, self.active_dims]
            if X2 is not None:
                X2 = X2[:, self.active_dims]
        else:
            X = tf.transpose(tf.gather(tf.transpose(X), self.active_dims))
            if X2 is not None:
                X2 = tf.transpose(tf.gather(tf.transpose(X2), self.active_dims))
        with tf.control_dependencies([
            tf.assert_equal(tf.shape(X)[1], tf.constant(self.input_dim, dtype=settings.dtypes.int_type))
        ]):
            X = tf.identity(X)

        return X, X2

    def _slice_cov(self, cov):
        """
        Slice the correct dimensions for use in the kernel, as indicated by
        `self.active_dims` for covariance matrices. This requires slicing the
        rows *and* columns. This will also turn flattened diagonal
        matrices into a tensor of full diagonal matrices.
        :param cov: Tensor of covariance matrices (NxDxD or NxD).
        :return: N x self.input_dim x self.input_dim.
        """
        cov = tf.cond(tf.equal(tf.rank(cov), 2), lambda: tf.matrix_diag(cov), lambda: cov)

        if isinstance(self.active_dims, slice):
            cov = cov[..., self.active_dims, self.active_dims]
        else:
            cov_shape = tf.shape(cov)
            covr = tf.reshape(cov, [-1, cov_shape[-1], cov_shape[-1]])
            gather1 = tf.gather(tf.transpose(covr, [2, 1, 0]), self.active_dims)
            gather2 = tf.gather(tf.transpose(gather1, [1, 0, 2]), self.active_dims)
            cov = tf.reshape(tf.transpose(gather2, [2, 0, 1]),
                             tf.concat([cov_shape[:-2], [len(self.active_dims), len(self.active_dims)]], 0))
        return cov

    def __add__(self, other):
        return Add([self, other])

    def __mul__(self, other):
        return Prod([self, other])

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]))
    def compute_K(self, X, Z):
        return self.K(X, Z)

    @AutoFlow((float_type, [None, None]))
    def compute_K_symm(self, X):
        return self.K(X)

    @AutoFlow((float_type, [None, None]))
    def compute_Kdiag(self, X):
        return self.Kdiag(X)

    @AutoFlow((float_type, [None, None]), (float_type,))
    def compute_eKdiag(self, X, Xcov=None):
        return self.eKdiag(X, Xcov)

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]), (float_type,))
    def compute_eKxz(self, Z, Xmu, Xcov):
        return self.eKxz(Z, Xmu, Xcov)

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]), (float_type, [None, None, None, None]))
    def compute_exKxz(self, Z, Xmu, Xcov):
        return self.exKxz(Z, Xmu, Xcov)

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]), (float_type,))
    def compute_eKzxKxz(self, Z, Xmu, Xcov):
        return self.eKzxKxz(Z, Xmu, Xcov)

    def _check_quadrature(self):
        if settings.numerics.ekern_quadrature == "warn":
            warnings.warn("Using numerical quadrature for kernel expectation of %s. Use gpflow.ekernels instead." %
                          str(type(self)))
        if settings.numerics.ekern_quadrature == "error" or self.num_gauss_hermite_points == 0:
            raise RuntimeError("Settings indicate that quadrature may not be used.")

    def eKdiag(self, Xmu, Xcov):
        """
        Computes <K_xx>_q(x).
        :param Xmu: Mean (NxD)
        :param Xcov: Covariance (NxDxD or NxD)
        :return: (N)
        """
        self._check_quadrature()
        Xmu, _ = self._slice(Xmu, None)
        Xcov = self._slice_cov(Xcov)
        return mvnquad(lambda x: self.Kdiag(x, presliced=True),
                       Xmu, Xcov,
                       self.num_gauss_hermite_points, self.input_dim)  # N

    def eKxz(self, Z, Xmu, Xcov):
        """
        Computes <K_xz>_q(x) using quadrature.
        :param Z: Fixed inputs (MxD).
        :param Xmu: X means (NxD).
        :param Xcov: X covariances (NxDxD or NxD).
        :return: (NxM)
        """
        self._check_quadrature()
        Xmu, Z = self._slice(Xmu, Z)
        Xcov = self._slice_cov(Xcov)
        M = tf.shape(Z)[0]
        return mvnquad(lambda x: self.K(x, Z, presliced=True), Xmu, Xcov, self.num_gauss_hermite_points,
                       self.input_dim, Dout=(M,))  # (H**DxNxD, H**D)

    def exKxz(self, Z, Xmu, Xcov):
        """
        Computes <x_{t-1} K_{x_t z}>_q(x) for each pair of consecutive X's in
        Xmu & Xcov.
        :param Z: Fixed inputs (MxD).
        :param Xmu: X means (T+1xD).
        :param Xcov: 2xT+1xDxD. [0, t, :, :] contains covariances for x_t. [1, t, :, :] contains the cross covariances
        for t and t+1.
        :return: (TxMxD).
        """
        self._check_quadrature()
        # Slicing is NOT needed here. The desired behaviour is to *still* return an NxMxD matrix. As even when the
        # kernel does not depend on certain inputs, the output matrix will still contain the outer product between the
        # mean of x_{t-1} and K_{x_t Z}. The code here will do this correctly automatically, since the quadrature will
        # still be done over the distribution x_{t-1, t}, only now the kernel will not depend on certain inputs.
        # However, this does mean that at the time of running this function we need to know the input *size* of Xmu, not
        # just `input_dim`.
        M = tf.shape(Z)[0]
        D = self.input_size if hasattr(self, 'input_size') else self.input_dim  # Number of actual input dimensions

        with tf.control_dependencies([
            tf.assert_equal(tf.shape(Xmu)[1], tf.constant(D, dtype=int_type),
                            message="Numerical quadrature needs to know correct shape of Xmu.")
        ]):
            Xmu = tf.identity(Xmu)

        # First, transform the compact representation of Xmu and Xcov into a
        # list of full distributions.
        fXmu = tf.concat((Xmu[:-1, :], Xmu[1:, :]), 1)  # Nx2D
        fXcovt = tf.concat((Xcov[0, :-1, :, :], Xcov[1, :-1, :, :]), 2)  # NxDx2D
        fXcovb = tf.concat((tf.transpose(Xcov[1, :-1, :, :], (0, 2, 1)), Xcov[0, 1:, :, :]), 2)
        fXcov = tf.concat((fXcovt, fXcovb), 1)
        return mvnquad(lambda x: tf.expand_dims(self.K(x[:, :D], Z), 2) *
                                 tf.expand_dims(x[:, D:], 1),
                       fXmu, fXcov, self.num_gauss_hermite_points,
                       2 * D, Dout=(M, D))

    def eKzxKxz(self, Z, Xmu, Xcov):
        """
        Computes <K_zx Kxz>_q(x).
        :param Z: Fixed inputs MxD.
        :param Xmu: X means (NxD).
        :param Xcov: X covariances (NxDxD or NxD).
        :return: NxMxM
        """
        self._check_quadrature()
        Xmu, Z = self._slice(Xmu, Z)
        Xcov = self._slice_cov(Xcov)
        M = tf.shape(Z)[0]

        def KzxKxz(x):
            Kxz = self.K(x, Z, presliced=True)
            return tf.expand_dims(Kxz, 2) * tf.expand_dims(Kxz, 1)

        return mvnquad(KzxKxz,
                       Xmu, Xcov, self.num_gauss_hermite_points,
                       self.input_dim, Dout=(M, M))


class Static(Kern):
    """
    Kernels who don't depend on the value of the inputs are 'Static'.  The only
    parameter is a variance.
    """

    def __init__(self, input_dim, variance=1.0, active_dims=None):
        Kern.__init__(self, input_dim, active_dims)
        self.variance = Param(variance, transforms.positive)

    def Kdiag(self, X):
        return tf.fill(tf.stack([tf.shape(X)[0]]), tf.squeeze(self.variance))


class White(Static):
    """
    The White kernel
    """

    def K(self, X, X2=None, presliced=False):
        if X2 is None:
            d = tf.fill(tf.stack([tf.shape(X)[0]]), tf.squeeze(self.variance))
            return tf.matrix_diag(d)
        else:
            shape = tf.stack([tf.shape(X)[0], tf.shape(X2)[0]])
            return tf.zeros(shape, float_type)


class Constant(Static):
    """
    The Constant (aka Bias) kernel
    """

    def K(self, X, X2=None, presliced=False):
        if X2 is None:
            shape = tf.stack([tf.shape(X)[0], tf.shape(X)[0]])
        else:
            shape = tf.stack([tf.shape(X)[0], tf.shape(X2)[0]])
        return tf.fill(shape, tf.squeeze(self.variance))


class Bias(Constant):
    """
    Another name for the Constant kernel, included for convenience.
    """
    pass


class Stationary(Kern):
    """
    Base class for kernels that are stationary, that is, they only depend on

        r = || x - x' ||

    This class handles 'ARD' behaviour, which stands for 'Automatic Relevance
    Determination'. This means that the kernel has one lengthscale per
    dimension, otherwise the kernel is isotropic (has a single lengthscale).
    """

    def __init__(self, input_dim, variance=1.0, lengthscales=None,
                 active_dims=None, ARD=False):
        """
        - input_dim is the dimension of the input to the kernel
        - variance is the (initial) value for the variance parameter
        - lengthscales is the initial value for the lengthscales parameter
          defaults to 1.0 (ARD=False) or np.ones(input_dim) (ARD=True).
        - active_dims is a list of length input_dim which controls which
          columns of X are used.
        - ARD specifies whether the kernel has one lengthscale per dimension
          (ARD=True) or a single lengthscale (ARD=False).
        """
        Kern.__init__(self, input_dim, active_dims)
        self.scoped_keys.extend(['square_dist', 'euclid_dist'])
        self.variance = Param(variance, transforms.positive)
        if ARD:
            if lengthscales is None:
                lengthscales = np.ones(input_dim, np_float_type)
            else:
                # accepts float or array:
                lengthscales = lengthscales * np.ones(input_dim, np_float_type)
            self.lengthscales = Param(lengthscales, transforms.positive)
            self.ARD = True
        else:
            if lengthscales is None:
                lengthscales = 1.0
            self.lengthscales = Param(lengthscales, transforms.positive)
            self.ARD = False

    def square_dist(self, X, X2):
        X = X / self.lengthscales
        Xs = tf.reduce_sum(tf.square(X), 1)
        if X2 is None:
            return -2 * tf.matmul(X, X, transpose_b=True) + \
                   tf.reshape(Xs, (-1, 1)) + tf.reshape(Xs, (1, -1))
        else:
            X2 = X2 / self.lengthscales
            X2s = tf.reduce_sum(tf.square(X2), 1)
            return -2 * tf.matmul(X, X2, transpose_b=True) + \
                   tf.reshape(Xs, (-1, 1)) + tf.reshape(X2s, (1, -1))

    def euclid_dist(self, X, X2):
        r2 = self.square_dist(X, X2)
        return tf.sqrt(r2 + 1e-12)

    def Kdiag(self, X, presliced=False):
        return tf.fill(tf.stack([tf.shape(X)[0]]), tf.squeeze(self.variance))


class RBF(Stationary):
    """
    The radial basis function (RBF) or squared exponential kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        return self.variance * tf.exp(-self.square_dist(X, X2) / 2)


class Linear(Kern):
    """
    The linear kernel
    """

    def __init__(self, input_dim, variance=1.0, active_dims=None, ARD=False):
        """
        - input_dim is the dimension of the input to the kernel
        - variance is the (initial) value for the variance parameter(s)
          if ARD=True, there is one variance per input
        - active_dims is a list of length input_dim which controls
          which columns of X are used.
        """
        Kern.__init__(self, input_dim, active_dims)
        self.ARD = ARD
        if ARD:
            # accept float or array:
            variance = np.ones(self.input_dim) * variance
            self.variance = Param(variance, transforms.positive)
        else:
            self.variance = Param(variance, transforms.positive)
        self.parameters = [self.variance]

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        if X2 is None:
            return tf.matmul(X * self.variance, X, transpose_b=True)
        else:
            return tf.matmul(X * self.variance, X2, transpose_b=True)

    def Kdiag(self, X, presliced=False):
        if not presliced:
            X, _ = self._slice(X, None)
        return tf.reduce_sum(tf.square(X) * self.variance, 1)


class Polynomial(Linear):
    """
    The Polynomial kernel. Samples are polynomials of degree `d`.
    """

    def __init__(self, input_dim, degree=3.0, variance=1.0, offset=1.0, active_dims=None, ARD=False):
        """
        :param input_dim: the dimension of the input to the kernel
        :param variance: the (initial) value for the variance parameter(s)
                         if ARD=True, there is one variance per input
        :param degree: the degree of the polynomial
        :param active_dims: a list of length input_dim which controls
          which columns of X are used.
        :param ARD: use variance as described
        """
        Linear.__init__(self, input_dim, variance, active_dims, ARD)
        self.degree = degree
        self.offset = Param(offset, transform=transforms.positive)

    def K(self, X, X2=None, presliced=False):
        return (Linear.K(self, X, X2, presliced=presliced) + self.offset) ** self.degree

    def Kdiag(self, X, presliced=False):
        return (Linear.Kdiag(self, X, presliced=presliced) + self.offset) ** self.degree


class Exponential(Stationary):
    """
    The Exponential kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * tf.exp(-0.5 * r)


class Matern12(Stationary):
    """
    The Matern 1/2 kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * tf.exp(-r)


class Matern32(Stationary):
    """
    The Matern 3/2 kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * (1. + np.sqrt(3.) * r) * \
               tf.exp(-np.sqrt(3.) * r)


class Matern52(Stationary):
    """
    The Matern 5/2 kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * (1.0 + np.sqrt(5.) * r + 5. / 3. * tf.square(r)) \
               * tf.exp(-np.sqrt(5.) * r)


class Cosine(Stationary):
    """
    The Cosine kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * tf.cos(r)


class ArcCosine(Kern):
    """
    The Arc-cosine family of kernels which mimics the computation in neural
    networks. The order parameter specifies the assumed activation function.
    The Multi Layer Perceptron (MLP) kernel is closely related to the ArcCosine
    kernel of order 0. The key reference is

    ::

        @incollection{NIPS2009_3628,
            title = {Kernel Methods for Deep Learning},
            author = {Youngmin Cho and Lawrence K. Saul},
            booktitle = {Advances in Neural Information Processing Systems 22},
            year = {2009},
            url = {http://papers.nips.cc/paper/3628-kernel-methods-for-deep-learning.pdf}
        }
    """

    implemented_orders = {0, 1, 2}
    def __init__(self, input_dim,
                 order=0,
                 variance=1.0, weight_variances=1., bias_variance=1.,
                 active_dims=None, ARD=False):
        """
        - input_dim is the dimension of the input to the kernel
        - order specifies the activation function of the neural network
          the function is a rectified monomial of the chosen order.
        - variance is the initial value for the variance parameter
        - weight_variances is the initial value for the weight_variances parameter
          defaults to 1.0 (ARD=False) or np.ones(input_dim) (ARD=True).
        - bias_variance is the initial value for the bias_variance parameter
          defaults to 1.0.
        - active_dims is a list of length input_dim which controls which
          columns of X are used.
        - ARD specifies whether the kernel has one weight_variance per dimension
          (ARD=True) or a single weight_variance (ARD=False).
        """
        Kern.__init__(self, input_dim, active_dims)

        if order not in self.implemented_orders:
            raise ValueError('Requested kernel order is not implemented.')
        self.order = order

        self.variance = Param(variance, transforms.positive)
        self.bias_variance = Param(bias_variance, transforms.positive)
        if ARD:
            if weight_variances is None:
                weight_variances = np.ones(input_dim, np_float_type)
            else:
                # accepts float or array:
                weight_variances = weight_variances * np.ones(input_dim, np_float_type)
            self.weight_variances = Param(weight_variances, transforms.positive)
            self.ARD = True
        else:
            if weight_variances is None:
                weight_variances = 1.0
            self.weight_variances = Param(weight_variances, transforms.positive)
            self.ARD = False

    def _weighted_product(self, X, X2=None):
        if X2 is None:
            return tf.reduce_sum(self.weight_variances * tf.square(X), axis=1) + self.bias_variance
        else:
            return tf.matmul((self.weight_variances * X), X2, transpose_b=True) + self.bias_variance

    def _J(self, theta):
        """
        Implements the order dependent family of functions defined in equations
        4 to 7 in the reference paper.
        """
        if self.order == 0:
            return np.pi - theta
        elif self.order == 1:
            return tf.sin(theta) + (np.pi - theta) * tf.cos(theta)
        elif self.order == 2:
            return 3. * tf.sin(theta) * tf.cos(theta) + \
                   (np.pi - theta) * (1. + 2. * tf.cos(theta) ** 2)

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)

        X_denominator = tf.sqrt(self._weighted_product(X))
        if X2 is None:
            X2 = X
            X2_denominator = X_denominator
        else:
            X2_denominator = tf.sqrt(self._weighted_product(X2))

        numerator = self._weighted_product(X, X2)
        cos_theta = numerator / X_denominator[:, None] / X2_denominator[None, :]
        jitter = 1e-15
        theta = tf.acos(jitter + (1 - 2 * jitter) * cos_theta)

        return self.variance * (1. / np.pi) * self._J(theta) * \
               X_denominator[:, None] ** self.order * \
               X2_denominator[None, :] ** self.order

    def Kdiag(self, X, presliced=False):
        if not presliced:
            X, _ = self._slice(X, None)

        X_product = self._weighted_product(X)
        theta = tf.constant(0., float_type)
        return self.variance * (1. / np.pi) * self._J(theta) * X_product ** self.order


class PeriodicKernel(Kern):
    """
    The periodic kernel. Defined in  Equation (47) of

    D.J.C.MacKay. Introduction to Gaussian processes. In C.M.Bishop, editor,
    Neural Networks and Machine Learning, pages 133--165. Springer, 1998.

    Derived using the mapping u=(cos(x), sin(x)) on the inputs.
    """

    def __init__(self, input_dim, period=1.0, variance=1.0,
                 lengthscales=1.0, active_dims=None):
        # No ARD support for lengthscale or period yet
        Kern.__init__(self, input_dim, active_dims)
        self.variance = Param(variance, transforms.positive)
        self.lengthscales = Param(lengthscales, transforms.positive)
        self.ARD = False
        self.period = Param(period, transforms.positive)

    def Kdiag(self, X, presliced=False):
        return tf.fill(tf.stack([tf.shape(X)[0]]), tf.squeeze(self.variance))

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        if X2 is None:
            X2 = X

        # Introduce dummy dimension so we can use broadcasting
        f = tf.expand_dims(X, 1)  # now N x 1 x D
        f2 = tf.expand_dims(X2, 0)  # now 1 x M x D

        r = np.pi * (f - f2) / self.period
        r = tf.reduce_sum(tf.square(tf.sin(r) / self.lengthscales), 2)

        return self.variance * tf.exp(-0.5 * r)


class Coregion(Kern):
    def __init__(self, input_dim, output_dim, rank, active_dims=None):
        """
        A Coregionalization kernel. The inputs to this kernel are _integers_
        (we cast them from floats as needed) which usually specify the
        *outputs* of a Coregionalization model.

        The parameters of this kernel, W, kappa, specify a positive-definite
        matrix B.

          B = W W^T + diag(kappa) .

        The kernel function is then an indexing of this matrix, so

          K(x, y) = B[x, y] .

        We refer to the size of B as "num_outputs x num_outputs", since this is
        the number of outputs in a coregionalization model. We refer to the
        number of columns on W as 'rank': it is the number of degrees of
        correlation between the outputs.

        NB. There is a symmetry between the elements of W, which creates a
        local minimum at W=0. To avoid this, it's recommended to initialize the
        optimization (or MCMC chain) using a random W.
        """
        assert input_dim == 1, "Coregion kernel in 1D only"
        Kern.__init__(self, input_dim, active_dims)

        self.output_dim = output_dim
        self.rank = rank
        self.W = Param(np.zeros((self.output_dim, self.rank)))
        self.kappa = Param(np.ones(self.output_dim), transforms.positive)

    def K(self, X, X2=None):
        X, X2 = self._slice(X, X2)
        X = tf.cast(X[:, 0], tf.int32)
        if X2 is None:
            X2 = X
        else:
            X2 = tf.cast(X2[:, 0], tf.int32)
        B = tf.matmul(self.W, self.W, transpose_b=True) + tf.matrix_diag(self.kappa)
        return tf.gather(tf.transpose(tf.gather(B, X2)), X)

    def Kdiag(self, X):
        X, _ = self._slice(X, None)
        X = tf.cast(X[:, 0], tf.int32)
        Bdiag = tf.reduce_sum(tf.square(self.W), 1) + self.kappa
        return tf.gather(Bdiag, X)


def make_kernel_names(kern_list):
    """
    Take a list of kernels and return a list of strings, giving each kernel a
    unique name.

    Each name is made from the lower-case version of the kernel's class name.

    Duplicate kernels are given trailing numbers.
    """
    names = []
    counting_dict = {}
    for k in kern_list:
        raw_name = k.__class__.__name__.lower()

        # check for duplicates: start numbering if needed
        if raw_name in counting_dict:
            if counting_dict[raw_name] == 1:
                names[names.index(raw_name)] = raw_name + '_1'
            counting_dict[raw_name] += 1
            name = raw_name + '_' + str(counting_dict[raw_name])
        else:
            counting_dict[raw_name] = 1
            name = raw_name
        names.append(name)
    return names


class Combination(Kern):
    """
    Combine  a list of kernels, e.g. by adding or multiplying (see inheriting
    classes).

    The names of the kernels to be combined are generated from their class
    names.
    """

    def __init__(self, kern_list):
        for k in kern_list:
            assert isinstance(k, Kern), "can only add Kern instances"

        input_dim = np.max([k.input_dim
                            if type(k.active_dims) is slice else
                            np.max(k.active_dims) + 1
                            for k in kern_list])
        Kern.__init__(self, input_dim=input_dim)

        # add kernels to a list, flattening out instances of this class therein
        self.kern_list = []
        for k in kern_list:
            if isinstance(k, self.__class__):
                self.kern_list.extend(k.kern_list)
            else:
                self.kern_list.append(k)

        # generate a set of suitable names and add the kernels as attributes
        names = make_kernel_names(self.kern_list)
        [setattr(self, name, k) for name, k in zip(names, self.kern_list)]

    @property
    def on_separate_dimensions(self):
        """
        Checks whether the kernels in the combination act on disjoint subsets
        of dimensions. Currently, it is hard to asses whether two slice objects
        will overlap, so this will always return False.
        :return: Boolean indicator.
        """
        if np.any([isinstance(k.active_dims, slice) for k in self.kern_list]):
            # Be conservative in the case of a slice object
            return False
        else:
            dimlist = [k.active_dims for k in self.kern_list]
            overlapping = False
            for i, dims_i in enumerate(dimlist):
                for dims_j in dimlist[i + 1:]:
                    if np.any(dims_i.reshape(-1, 1) == dims_j.reshape(1, -1)):
                        overlapping = True
            return not overlapping


class Add(Combination):
    def K(self, X, X2=None, presliced=False):
        return reduce(tf.add, [k.K(X, X2) for k in self.kern_list])

    def Kdiag(self, X, presliced=False):
        return reduce(tf.add, [k.Kdiag(X) for k in self.kern_list])


class Prod(Combination):
    def K(self, X, X2=None, presliced=False):
        return reduce(tf.multiply, [k.K(X, X2) for k in self.kern_list])

    def Kdiag(self, X, presliced=False):
        return reduce(tf.multiply, [k.Kdiag(X) for k in self.kern_list])


class DifferentialObservationsKernelPreDefined(Kern):
    """
    Differential kernels
    These are kernels between observations of the function and
    observation of function derivatives. see eg:
    http://mlg.eng.cam.ac.uk/pub/pdf/SolMurLeietal03.pdf
    Solak, Ercan, et al. "Derivative observations in Gaussian process models of dynamic systems.
    " Advances in neural information processing systems. 2003.

    This particular implementation should work on any existing kernel (assuming that it can
    be differentiated) however due to TensorFlow's static graph you have to define which
    observations will be derivatives before feeding data (at compile time).
    The derivative information takes the form of a matrix for each item in the kernel:
    |---------------------------------------------------|---------|
    | numberderivs | first_deriv_dim | second_deriv_dim | etc ... |

    So for as an example say we have the following two dimenional data matrix:
    [[x_a1, x_a2],
     [x_b1, x_b2],
     [x_c1, x_c2]]

    Then the derivative information matrix
    [[2, 0, 1],
     [1, 1, -1],
     [0, -1, -1]]

    would mean the observations corresponding to these data points are:
    [ d^2f_1/(dx_a1 dx_a2), df_2/dx_b2, f_3].

    -1s can be used as fillers in the derivative information matrix. But alternatively they can be
    left blank.
    """
    def __init__(self, input_dim, base_kernel, deriv_info_x, deriv_info_x2, active_dims=None):

        Kern.__init__(self, input_dim, active_dims)
        self.base_kernel = base_kernel
        self.deriv_info_x = deriv_info_x
        self.deriv_info_x2 = deriv_info_x2

    def __setattr__(self, key, value):
        try:
            if key in {self.deriv_info_x, self.deriv_info_x2}:
                self.highest_parent._needs_recompile = True
        except (AttributeError, TypeError):
            pass
        Kern.__setattr__(self, key, value)

    def K(self, X, X2=None):
        # Split X up into two separate vectors (do this as when we do tf.gradients
        # we only actually want to differentiate
        if X2 is None:
            X2 = tf.identity(X)
            X = tf.identity(X)
            deriv_info_x2 = self.deriv_info_x
        else:
            deriv_info_x2 = self.deriv_info_x2

        # Compute the kernel assuming no gradient observations
        raw_kernel = self.base_kernel.K(X, X2)

        # Go through and make sure that each point actually has correct derivative points
        output = []
        for i in range(int(self.deriv_info_x.shape[0])):
            for j in range(int(deriv_info_x2.shape[0])):
                k_new = raw_kernel[i, j]
                num_derivs_left = self.deriv_info_x[i, 0]
                deriv_order_left = self.deriv_info_x[i, 1:]
                for _, d in zip(range(num_derivs_left), deriv_order_left):
                    k_new = tf.gradients(k_new, X)[0][i, d]

                num_derivs_right = deriv_info_x2[j, 0]
                deriv_order_right = deriv_info_x2[j, 1:]
                for _, d in zip(range(num_derivs_right), deriv_order_right):
                    k_new = tf.gradients(k_new, X2)[0][j, d]
                output.append(k_new)
        full_new_k = tf.stack(output)
        full_new_k_correct_shape = tf.reshape(full_new_k, tf.shape(raw_kernel))
        return full_new_k_correct_shape

    def Kdiag(self, X):
        k = self.K(X)
        # So we have to solve the diag going through the whole K matrix, as some of the Kdiag
        # implementations make simplifications which means that the gradients will not be correct.
        # For instance the stationary kernel returns just the variances so as X is ignored
        # it will not differentiate properly
        return tf.diag_part(k)


class DifferentialObservationsKernelDynamic(Kern):
    """
    Differential kernels
    These are kernels between observations of the function and
    observation of function derivatives. see eg:
    http://mlg.eng.cam.ac.uk/pub/pdf/SolMurLeietal03.pdf
    Solak, Ercan, et al. "Derivative observations in Gaussian process models of dynamic systems.
    " Advances in neural information processing systems. 2003.

    This particular kernel can work with dynamically defined observations. In other words one
    can define after building the graph the observations. Because TensorFlow has to have a static
    graph this class has to build the possible combinations of derivatives at compile time.
    A switch is then used to pick the correct portion of the graph to evaluate at run time.
    We have only defined the kernel to deal with upto second order derivatives.

    When feeding the data to the GP one must define an extra dimension. This defines
    the number of derivatives in this corresponding dimension direction the corresponding
    observation records.

    For instance the x tensor:
    x = [[a,b,0,1],
         [c,d,0,0],
         [e,f,2,0]]

    would mean you have seen three observations at [a,b], [c,d] and [e,f].
    and these observations will be respectively
    df/dx_2|x=[a,b]
    f|x=[c,d]
    and d^2f/(dx_1^2)|x=[e,f].
    (you have to pass into this class the variable obs_dims, which denotes the number of dimensions
    of the observations, in this case 2).

    Undefined behaviour if following condiitions not met:
        * gradient denotation should be positive integers
        * only can do up to second derivatives
    """

    def __init__(self, input_dim, base_kernel, obs_dims, active_dims=None):
        Kern.__init__(self, input_dim, active_dims)
        self.obs_dims = obs_dims
        self.base_kernel = base_kernel


    def K(self, X, X2=None):
        # Split X up into two separate vectors (do this as when we do tf.gradients
        # we only actually want to differentiate
        if X2 is None:
            X2 = tf.identity(X)
            X = tf.identity(X)

        x1, d1 = self._split_x_into_locs_and_grad_information(X)
        x2, d2 = self._split_x_into_locs_and_grad_information(X2)

        d1 = self._convert_grad_info_into_indics(d1)
        d2 = self._convert_grad_info_into_indics(d2)


        # Compute the kernel assuming no gradient observations
        raw_kernel = self.base_kernel.K(x1, x2)

        new_k = self._k_correct_dynamic(raw_kernel, x1, x2, d1, d2)
        return new_k

    def Kdiag(self, X):
        k = self.K(X)
        # So we have to solve the diag going through the whole K matrix, as some of the Kdiag
        # implementations make simplifications which means that the gradients will not be correct.
        # For instance the stationary kernel returns just the variances so as X is ignored
        # it will not differentiate properly
        return tf.diag_part(k)

    def _split_x_into_locs_and_grad_information(self, x):
        locs = x[:, :self.obs_dims]
        grad_info = x[:, -self.obs_dims:]
        return locs, grad_info

    def _convert_grad_info_into_indics(self, grad_info_matrix):
        """
        This function takes gradient information in the form given to the class -- ie an
        integer mask telling how many times the gradient has been taken in that direction and
        converts it to the derivative information matrix form.
        An example derivative information matrix
        [[2, 0, 1],
         [1, 1, -1],
         [0, -1, -1]]

        would mean the observations corresponding to these data points are:
        [ d^2f_1/(dx_a1 dx_a2), df_2/dx_b2, f_3].

        We assume that the maximum number of derivatives will be two but do not check this so
        undefined behaviour if you have given more than two.
        """
        deriv_info_matrix = tf.to_int32(grad_info_matrix)
        number_grads = tf.reduce_sum(deriv_info_matrix, axis=1)

        first_index = tf.argmax(deriv_info_matrix, axis=1, output_type=tf.int32)

        # Having worked out where the first derivative is taken from we
        #  now remove it from the records.
        remaining = deriv_info_matrix - tf.one_hot(first_index, depth=tf.shape(deriv_info_matrix)[1],
                                                   dtype=tf.int32)

        second_index = tf.argmax(remaining, axis=1, output_type=tf.int32)

        deriv_info_matrix = tf.transpose(tf.stack((number_grads, first_index, second_index), axis=0))
        return deriv_info_matrix

    def _k_correct_dynamic(self, k, xl, xr, deriv_info_left, deriv_info_right):
        k_shape = tf.shape(k)
        k_orig = k

        indcs_x1 = tf.range(0, tf.shape(xl)[0])[:, None] + tf.zeros(tf.shape(k), dtype=tf.int32)
        indcs_x2 = tf.range(0, tf.shape(xr)[0])[None, :] + tf.zeros(tf.shape(k), dtype=tf.int32)

        elems = [tf.reshape(t, (-1,)) for t in (indcs_x1, indcs_x2)]

        def calc_derivs(tensor_in):
            idxl = tensor_in[0]
            idxr = tensor_in[1]

            k = k_orig[idxl, idxr]

            idx_i = deriv_info_left[idxl, 1]
            idx_j = deriv_info_left[idxl, 2]
            idx_k = deriv_info_right[idxr, 1]
            idx_m = deriv_info_right[idxr, 2]

            # First order derivatives
            dk__dxli = lambda: tf.gradients(k, xl)[0][idxl, idx_i]
            dk__dxrk = lambda: tf.gradients(k, xr)[0][idxr, idx_k]

            # Second order derivatives
            dk__dxlj_dxli_ = tf.gradients(dk__dxli(), xl)[0][idxl, idx_j]
            dk__dxli_dxrk_ = tf.gradients(dk__dxrk(), xl)[0][idxl, idx_i]
            dk__dxrm_dxrk_ = tf.gradients(dk__dxrk(), xr)[0][idxr, idx_m]
            dk__dxlj_dxli = lambda: dk__dxlj_dxli_
            dk__dxli_dxrk = lambda: dk__dxli_dxrk_
            dk__dxrm_dxrk = lambda: dk__dxrm_dxrk_

            # Third order derivatives
            dk__dxlj_dxli_dxrk = lambda: tf.gradients(dk__dxli_dxrk_, xl)[0][idxl, idx_j]
            dk__dxli_dxrm_dxrk = lambda: tf.gradients(dk__dxrm_dxrk_, xl)[0][idxl, idx_i]

            # Fourth order derivatives
            dk__dxlj_dxli_dxrm_dxrk = lambda: tf.gradients(dk__dxli_dxrm_dxrk(), xl)[0][idxl, idx_j]

            num_left_derivs = deriv_info_left[idxl, 0]
            num_right_derivs = deriv_info_right[idxr, 0]
            k_new = tf.case(
                [
                    # Zeroth order
                    # ... is done by default
                    # First order
                    (tf.logical_and(tf.equal(num_left_derivs, 1), tf.equal(num_right_derivs, 0)),
                     dk__dxli),
                    (tf.logical_and(tf.equal(num_left_derivs, 0), tf.equal(num_right_derivs, 1)),
                     dk__dxrk),
                    # Second order
                    (tf.logical_and(tf.equal(num_left_derivs, 2), tf.equal(num_right_derivs, 0)),
                     dk__dxlj_dxli),
                    (tf.logical_and(tf.equal(num_left_derivs, 1), tf.equal(num_right_derivs, 1)),
                     dk__dxli_dxrk),
                    (tf.logical_and(tf.equal(num_left_derivs, 0), tf.equal(num_right_derivs, 2)),
                     dk__dxrm_dxrk),
                    # Third order
                    (tf.logical_and(tf.equal(num_left_derivs, 2), tf.equal(num_right_derivs, 1)),
                     dk__dxlj_dxli_dxrk),
                    (tf.logical_and(tf.equal(num_left_derivs, 1), tf.equal(num_right_derivs, 2)),
                     dk__dxli_dxrm_dxrk),
                    # Fourth order
                    (tf.logical_and(tf.equal(num_left_derivs, 2), tf.equal(num_right_derivs, 2)),
                     dk__dxlj_dxli_dxrm_dxrk),
                ], default=lambda: k, exclusive=True
            )

            return k_new

        new_kernel = tf.map_fn(calc_derivs, elems, dtype=tf.float64)
        new_kernel_reshaped = tf.reshape(new_kernel, k_shape)
        return new_kernel_reshaped

