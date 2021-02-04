"""
The :mod:`tslearn.clustering` module gathers time series specific clustering
algorithms.

**User guide:** See the :ref:`Clustering <clustering>` section for further
 details.
"""

from sklearn.base import ClusterMixin, TransformerMixin
from sklearn.metrics.pairwise import pairwise_kernels
try:
    # Most recent
    from sklearn.cluster._k_means import _k_init
except ImportError:
    # Deprecated from sklearn v0.24 onwards
    from sklearn.cluster.k_means_ import _k_init
from sklearn.metrics.cluster import \
    silhouette_score as sklearn_silhouette_score
from sklearn.utils import check_random_state
from sklearn.utils.extmath import stable_cumsum
from sklearn.utils.validation import _check_sample_weight
from scipy.spatial.distance import cdist
import numpy
import warnings

from tslearn.metrics import cdist_gak, cdist_dtw, cdist_soft_dtw, \
    cdist_soft_dtw_normalized, sigma_gak
from tslearn.barycenters import euclidean_barycenter, \
    dtw_barycenter_averaging, softdtw_barycenter
from sklearn.utils import check_array
from sklearn.utils.validation import check_is_fitted
from tslearn.preprocessing import TimeSeriesScalerMeanVariance, \
    TimeSeriesResampler
from tslearn.utils import (to_time_series_dataset, to_time_series,
                           to_sklearn_dataset, check_dims)
from tslearn.cycc import cdist_normalized_cc, y_shifted_sbd_vec
from tslearn.bases import BaseModelPackage, TimeSeriesBaseEstimator

__author__ = 'Romain Tavenard romain.tavenard[at]univ-rennes2.fr'
# Kernel k-means is derived from https://gist.github.com/mblondel/6230787 by
# Mathieu Blondel, under BSD 3 clause license


def _k_init_metric(X, n_clusters, cdist_metric, random_state,
                   n_local_trials=None):
    """Init n_clusters seeds according to k-means++ with a custom distance
    metric.

    Parameters
    ----------
    X : array, shape (n_samples, n_timestamps, n_features)
        The data to pick seeds for.

    n_clusters : integer
        The number of seeds to choose

    cdist_metric : function
        Function to be called for cross-distance computations

    random_state : RandomState instance
        Generator used to initialize the centers.

    n_local_trials : integer, optional
        The number of seeding trials for each center (except the first),
        of which the one reducing inertia the most is greedily chosen.
        Set to None to make the number of trials depend logarithmically
        on the number of seeds (2+log(k)); this is the default.

    Notes
    -----
    Selects initial cluster centers for k-mean clustering in a smart way
    to speed up convergence. see: Arthur, D. and Vassilvitskii, S.
    "k-means++: the advantages of careful seeding". ACM-SIAM symposium
    on Discrete algorithms. 2007

    Version adapted from scikit-learn for use with a custom metric in place of
    Euclidean distance.
    """
    n_samples, n_timestamps, n_features = X.shape

    centers = numpy.empty((n_clusters, n_timestamps, n_features),
                          dtype=X.dtype)

    # Set the number of local seeding trials if none is given
    if n_local_trials is None:
        # This is what Arthur/Vassilvitskii tried, but did not report
        # specific results for other than mentioning in the conclusion
        # that it helped.
        n_local_trials = 2 + int(numpy.log(n_clusters))

    # Pick first center randomly
    center_id = random_state.randint(n_samples)
    centers[0] = X[center_id]

    # Initialize list of closest distances and calculate current potential
    closest_dist_sq = cdist_metric(centers[0, numpy.newaxis], X) ** 2
    current_pot = closest_dist_sq.sum()

    # Pick the remaining n_clusters-1 points
    for c in range(1, n_clusters):
        # Choose center candidates by sampling with probability proportional
        # to the squared distance to the closest existing center
        rand_vals = random_state.random_sample(n_local_trials) * current_pot
        candidate_ids = numpy.searchsorted(stable_cumsum(closest_dist_sq),
                                           rand_vals)
        # XXX: numerical imprecision can result in a candidate_id out of range
        numpy.clip(candidate_ids, None, closest_dist_sq.size - 1,
                   out=candidate_ids)

        # Compute distances to center candidates
        distance_to_candidates = cdist_metric(X[candidate_ids], X) ** 2

        # update closest distances squared and potential for each candidate
        numpy.minimum(closest_dist_sq, distance_to_candidates,
                      out=distance_to_candidates)
        candidates_pot = distance_to_candidates.sum(axis=1)

        # Decide which candidate is the best
        best_candidate = numpy.argmin(candidates_pot)
        current_pot = candidates_pot[best_candidate]
        closest_dist_sq = distance_to_candidates[best_candidate]
        best_candidate = candidate_ids[best_candidate]

        # Permanently add best center candidate found in local tries
        centers[c] = X[best_candidate]

    return centers


class EmptyClusterError(Exception):
    def __init__(self, message=""):
        super().__init__()
        self.message = message

    def __str__(self):
        if len(self.message) > 0:
            suffix = " (%s)" % self.message
        else:
            suffix = ""
        return "Cluster assignments lead to at least one empty cluster" + \
               suffix


def _check_no_empty_cluster(labels, n_clusters):
    """Check that all clusters have at least one sample assigned.
    """

    for k in range(n_clusters):
        if numpy.sum(labels == k) == 0:
            raise EmptyClusterError


def _check_full_length(centroids):
    """Check that provided centroids are full-length (ie. not padded with
    nans).

    If some centroids are found to be padded with nans, TimeSeriesResampler is
    used to resample the centroids.
    """
    resampler = TimeSeriesResampler(sz=centroids.shape[1])
    return resampler.fit_transform(centroids)


def _compute_inertia(distances, assignments, squared=True):
    """Derive inertia (average of squared distances) from pre-computed
    distances and assignments.

    Examples
    --------
    >>> dists = numpy.array([[1., 2., 0.5], [0., 3., 1.]])
    >>> assign = numpy.array([2, 0])
    >>> _compute_inertia(dists, assign)
    0.125
    """
    n_ts = distances.shape[0]
    if squared:
        return numpy.sum(distances[numpy.arange(n_ts),
                                   assignments] ** 2) / n_ts
    else:
        return numpy.sum(distances[numpy.arange(n_ts), assignments]) / n_ts


def silhouette_score(X, labels, metric=None, sample_size=None,
                     metric_params=None, n_jobs=None, verbose=0,
                     random_state=None, **kwds):
    """Compute the mean Silhouette Coefficient of all samples (cf.  [1]_ and
    [2]_).

    Read more in the `scikit-learn documentation
    <http://scikit-learn.org/stable/modules/clustering.html\
    #silhouette-coefficient>`_.

    Parameters
    ----------
    X : array [n_ts, n_ts] if metric == "precomputed", or, \
             [n_ts, sz, d] otherwise
        Array of pairwise distances between time series, or a time series
        dataset.
    labels : array, shape = [n_ts]
         Predicted labels for each time series.
    metric : string, callable or None (default: None)
        The metric to use when calculating distance between time series.
        Should be one of {'dtw', 'softdtw', 'euclidean'} or a callable distance
        function or None.
        If 'softdtw' is passed, a normalized version of Soft-DTW is used that
        is defined as `sdtw_(x,y) := sdtw(x,y) - 1/2(sdtw(x,x)+sdtw(y,y))`.
        If X is the distance array itself, use ``metric="precomputed"``.
        If None, dtw is used.
    sample_size : int or None (default: None)
        The size of the sample to use when computing the Silhouette Coefficient
        on a random subset of the data.
        If ``sample_size is None``, no sampling is used.
    metric_params : dict or None (default: None)
        Parameter values for the chosen metric.
        For metrics that accept parallelization of the cross-distance matrix
        computations, `n_jobs` key passed in `metric_params` is overridden by
        the `n_jobs` argument.

    n_jobs : int or None, optional (default=None)
        The number of jobs to run in parallel for cross-distance matrix
        computations.
        Ignored if the cross-distance matrix cannot be computed using
        parallelization.
        ``None`` means 1 unless in a :obj:`joblib.parallel_backend` context.
        ``-1`` means using all processors. See scikit-learns'
        `Glossary <https://scikit-learn.org/stable/glossary.html#term-n-jobs>`_
        for more details.

    verbose : int (default: 0)
        If nonzero, print information about the inertia while learning
        the model and joblib progress messages are printed.

    random_state : int, RandomState instance or None, optional (default: None)
        The generator used to randomly select a subset of samples.  If int,
        random_state is the seed used by the random number generator; If
        RandomState instance, random_state is the random number generator; If
        None, the random number generator is the RandomState instance used by
        `np.random`. Used when ``sample_size is not None``.
    **kwds : optional keyword parameters
        Any further parameters are passed directly to the distance function,
        just as for the `metric_params` parameter.

    Returns
    -------
    silhouette : float
        Mean Silhouette Coefficient for all samples.

    References
    ----------
    .. [1] `Peter J. Rousseeuw (1987). "Silhouettes: a Graphical Aid to the
       Interpretation and Validation of Cluster Analysis". Computational
       and Applied Mathematics 20: 53-65.
       <http://www.sciencedirect.com/science/article/pii/0377042787901257>`_
    .. [2] `Wikipedia entry on the Silhouette Coefficient
           <https://en.wikipedia.org/wiki/Silhouette_(clustering)>`_

    Examples
    --------
    >>> from tslearn.generators import random_walks
    >>> from tslearn.metrics import cdist_dtw
    >>> numpy.random.seed(0)
    >>> X = random_walks(n_ts=20, sz=16, d=1)
    >>> labels = numpy.random.randint(2, size=20)
    >>> silhouette_score(X, labels, metric="dtw")  # doctest: +ELLIPSIS
    0.13383800...
    >>> silhouette_score(X, labels, metric="euclidean")  # doctest: +ELLIPSIS
    0.09126917...
    >>> silhouette_score(X, labels, metric="softdtw")  # doctest: +ELLIPSIS
    0.17953934...
    >>> silhouette_score(X, labels, metric="softdtw",
    ...                  metric_params={"gamma": 2.}) \
    # doctest: +ELLIPSIS
    0.17591060...
    >>> silhouette_score(cdist_dtw(X), labels,
    ...                  metric="precomputed")  # doctest: +ELLIPSIS
    0.13383800...
    """
    sklearn_metric = None
    if metric_params is None:
        metric_params_ = {}
    else:
        metric_params_ = metric_params.copy()
    for k in kwds.keys():
        metric_params_[k] = kwds[k]
    if "n_jobs" in metric_params_.keys():
        del metric_params_["n_jobs"]
    if metric == "precomputed":
        sklearn_X = X
    elif metric == "dtw" or metric is None:
        sklearn_X = cdist_dtw(X, n_jobs=n_jobs, verbose=verbose,
                              **metric_params_)
    elif metric == "softdtw":
        sklearn_X = cdist_soft_dtw_normalized(X, **metric_params_)
    elif metric == "euclidean":
        X_ = to_time_series_dataset(X)
        X_ = X_.reshape((X.shape[0], -1))
        sklearn_X = cdist(X_, X_, metric="euclidean")
    else:
        X_ = to_time_series_dataset(X)
        n, sz, d = X_.shape
        sklearn_X = X_.reshape((n, -1))

        def sklearn_metric(x, y):
            return metric(to_time_series(x.reshape((sz, d)),
                                         remove_nans=True),
                          to_time_series(y.reshape((sz, d)),
                                         remove_nans=True))
    metric = "precomputed" if sklearn_metric is None else sklearn_metric
    return sklearn_silhouette_score(X=sklearn_X,
                                    labels=labels,
                                    metric=metric,
                                    sample_size=sample_size,
                                    random_state=random_state,
                                    **kwds)


def _check_initial_guess(init, n_clusters):
    if hasattr(init, '__array__'):
        assert init.shape[0] == n_clusters, \
            "Initial guess index array must contain {} samples," \
            " {} given".format(n_clusters, init.shape[0])


class KernelKMeans(ClusterMixin, BaseModelPackage, TimeSeriesBaseEstimator):
    """Kernel K-means.

    Parameters
    ----------
    n_clusters : int (default: 3)
        Number of clusters to form.

    kernel : string, or callable (default: "gak")
        The kernel should either be "gak", in which case the Global Alignment
        Kernel from [2]_ is used or a value that is accepted as a metric
        by `scikit-learn's pairwise_kernels
        <https://scikit-learn.org/stable/modules/generated/\
        sklearn.metrics.pairwise.pairwise_kernels.html>`_

    max_iter : int (default: 50)
        Maximum number of iterations of the k-means algorithm for a single run.

    tol : float (default: 1e-6)
        Inertia variation threshold. If at some point, inertia varies less than
        this threshold between two consecutive
        iterations, the model is considered to have converged and the algorithm
        stops.

    n_init : int (default: 1)
        Number of time the k-means algorithm will be run with different
        centroid seeds. The final results will be the
        best output of n_init consecutive runs in terms of inertia.

    kernel_params : dict or None (default: None)
        Kernel parameters to be passed to the kernel function.
        None means no kernel parameter is set.
        For Global Alignment Kernel, the only parameter of interest is `sigma`.
        If set to 'auto', it is computed based on a sampling of the training
        set
        (cf :ref:`tslearn.metrics.sigma_gak <fun-tslearn.metrics.sigma_gak>`).
        If no specific value is set for `sigma`, its defaults to 1.

    sigma : float or "auto" (default: "auto")
        Bandwidth parameter for the Global Alignment kernel. If set to 'auto',
        it is computed based on a sampling of the training set
        (cf :ref:`tslearn.metrics.sigma_gak <fun-tslearn.metrics.sigma_gak>`)

        .. deprecated:: 0.4
            Setting `sigma` directly as a parameter for KernelKMeans and
            GlobalAlignmentKernelKMeans is deprecated in version 0.4 and will
            be removed in 0.6. Use `kernel_params` instead.

    n_jobs : int or None, optional (default=None)
        The number of jobs to run in parallel for GAK cross-similarity matrix
        computations.
        ``None`` means 1 unless in a :obj:`joblib.parallel_backend` context.
        ``-1`` means using all processors. See scikit-learns'
        `Glossary <https://scikit-learn.org/stable/glossary.html#term-n-jobs>`_
        for more details.

    verbose : int (default: 0)
        If nonzero, joblib progress messages are printed.

    random_state : integer or numpy.RandomState, optional
        Generator used to initialize the centers. If an integer is given, it
        fixes the seed. Defaults to the global
        numpy random number generator.

    Attributes
    ----------
    labels_ : numpy.ndarray
        Labels of each point

    inertia_ : float
        Sum of distances of samples to their closest cluster center (computed
        using the kernel trick).

    sample_weight_ : numpy.ndarray
        The weight given to each sample from the data provided to fit.

    n_iter_ : int
        The number of iterations performed during fit.

    Notes
    -----
        The training data are saved to disk if this model is
        serialized and may result in a large model file if the training
        dataset is large.

    Examples
    --------
    >>> from tslearn.generators import random_walks
    >>> X = random_walks(n_ts=50, sz=32, d=1)
    >>> gak_km = KernelKMeans(n_clusters=3, kernel="gak", random_state=0)
    >>> gak_km.fit(X)  # doctest: +ELLIPSIS
    KernelKMeans(...)
    >>> print(numpy.unique(gak_km.labels_))
    [0 1 2]

    References
    ----------
    .. [1] Kernel k-means, Spectral Clustering and Normalized Cuts.
           Inderjit S. Dhillon, Yuqiang Guan, Brian Kulis. KDD 2004.

    .. [2] Fast Global Alignment Kernels. Marco Cuturi. ICML 2011.
    """

    def __init__(self, n_clusters=3, kernel="gak", max_iter=50, tol=1e-6,
                 n_init=1, kernel_params=None, sigma=1., n_jobs=None,
                 verbose=0, random_state=None):
        self.n_clusters = n_clusters
        self.kernel = kernel
        self.max_iter = max_iter
        self.tol = tol
        self.n_init = n_init
        self.kernel_params = kernel_params
        self.sigma = sigma
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.random_state = random_state

    def _is_fitted(self):
        check_is_fitted(self, '_X_fit')
        return True

    def _get_model_params(self):
        params = super()._get_model_params()
        params.update({'_X_fit': self._X_fit})
        return params

    def _get_kernel_params(self):
        if self.kernel_params is None:
            kernel_params = {}
        else:
            kernel_params = self.kernel_params
        if self.kernel == "gak":
            if hasattr(self, "sigma_gak_"):
                kernel_params["sigma"] = self.sigma_gak_
            elif "sigma" not in kernel_params.keys():
                kernel_params["sigma"] = self.sigma
        return kernel_params

    def _get_kernel(self, X, Y=None):
        kernel_params = self._get_kernel_params()
        if self.kernel == "gak":
            return cdist_gak(X, Y, n_jobs=self.n_jobs, verbose=self.verbose,
                             **kernel_params)
        else:
            X_sklearn = to_sklearn_dataset(X)
            if Y is not None:
                Y_sklearn = to_sklearn_dataset(Y)
            else:
                Y_sklearn = Y
            return pairwise_kernels(X_sklearn, Y_sklearn, metric=self.kernel,
                                    n_jobs=self.n_jobs, **kernel_params)

    def _fit_one_init(self, K, rs):
        n_samples = K.shape[0]

        self.labels_ = rs.randint(self.n_clusters, size=n_samples)

        dist = numpy.empty((n_samples, self.n_clusters))
        old_inertia = numpy.inf

        for it in range(self.max_iter):
            dist.fill(0)
            self._compute_dist(K, dist)
            self.labels_ = dist.argmin(axis=1)
            _check_no_empty_cluster(self.labels_, self.n_clusters)
            self.inertia_ = self._compute_inertia(dist)
            if self.verbose:
                print("%.3f" % self.inertia_, end=" --> ")

            if numpy.abs(old_inertia - self.inertia_) < self.tol:
                break
            old_inertia = self.inertia_
        if self.verbose:
            print("")

        self._iter = it + 1

        return self

    def fit(self, X, y=None, sample_weight=None):
        """Compute kernel k-means clustering.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset.

        y
            Ignored
        sample_weight : array-like of shape=(n_ts, ) or None (default: None)
            Weights to be given to time series in the learning process. By
            default, all time series weights are equal.
        """
        if self.sigma != 1.:
            warnings.warn(
                "Setting `sigma` directly as a parameter for KernelKMeans "
                "and GlobalAlignmentKernelKMeans is deprecated in version "
                "0.4 and will be removed in 0.6. Use `kernel_params` "
                "instead.",
                DeprecationWarning, stacklevel=2)

        X = check_array(X, allow_nd=True, force_all_finite=False)
        X = check_dims(X)

        sample_weight = _check_sample_weight(sample_weight=sample_weight, X=X)

        max_attempts = max(self.n_init, 10)
        kernel_params = self._get_kernel_params()
        if self.kernel == "gak":
            self.sigma_gak_ = kernel_params.get("sigma", 1.)
            if self.sigma_gak_ == "auto":
                self.sigma_gak_ = sigma_gak(X)
        else:
            self.sigma_gak_ = None

        self.labels_ = None
        self.inertia_ = None
        self.sample_weight_ = None
        self._X_fit = None
        # n_iter_ will contain the number of iterations the most
        # successful run required.
        self.n_iter_ = 0

        n_samples = X.shape[0]
        K = self._get_kernel(X)
        sw = (sample_weight if sample_weight is not None
              else numpy.ones(n_samples))
        self.sample_weight_ = sw
        rs = check_random_state(self.random_state)

        last_correct_labels = None
        min_inertia = numpy.inf
        n_attempts = 0
        n_successful = 0
        while n_successful < self.n_init and n_attempts < max_attempts:
            try:
                if self.verbose and self.n_init > 1:
                    print("Init %d" % (n_successful + 1))
                n_attempts += 1
                self._fit_one_init(K, rs)
                if self.inertia_ < min_inertia:
                    last_correct_labels = self.labels_
                    min_inertia = self.inertia_
                    self.n_iter_ = self._iter
                n_successful += 1
            except EmptyClusterError:
                if self.verbose:
                    print("Resumed because of empty cluster")
        if n_successful > 0:
            self.labels_ = last_correct_labels
            self.inertia_ = min_inertia
            self._X_fit = X
        return self

    def _compute_dist(self, K, dist):
        """Compute a n_samples x n_clusters distance matrix using the kernel
        trick."""
        sw = self.sample_weight_

        for j in range(self.n_clusters):
            mask = (self.labels_ == j)

            if numpy.sum(mask) == 0:
                raise EmptyClusterError("try smaller n_cluster or better "
                                        "kernel parameters")

            # NB: we use a normalized kernel so k(x,x) = 1 for all x
            # (including the centroid)
            dist[:, j] = 2 - 2 * numpy.sum(sw[mask] * K[:, mask],
                                           axis=1) / sw[mask].sum()

    @staticmethod
    def _compute_inertia(dist_sq):
        return dist_sq.min(axis=1).sum()

    def fit_predict(self, X, y=None):
        """Fit kernel k-means clustering using X and then predict the closest
        cluster each time series in X belongs to.

        It is more efficient to use this method than to sequentially call fit
        and predict.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset to predict.

        y
            Ignored

        Returns
        -------
        labels : array of shape=(n_ts, )
            Index of the cluster each sample belongs to.
        """
        return self.fit(X, y).labels_

    def predict(self, X):
        """Predict the closest cluster each time series in X belongs to.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset to predict.

        Returns
        -------
        labels : array of shape=(n_ts, )
            Index of the cluster each sample belongs to.
        """
        X = check_array(X, allow_nd=True, force_all_finite=False)
        check_is_fitted(self, '_X_fit')
        X = check_dims(X, X_fit_dims=self._X_fit.shape,
                       check_n_features_only=True)
        K = self._get_kernel(X, self._X_fit)
        n_samples = X.shape[0]
        dist = numpy.zeros((n_samples, self.n_clusters))
        self._compute_dist(K, dist)
        return dist.argmin(axis=1)

    def _more_tags(self):
        return {'allow_nan': True, 'allow_variable_length': True}


class GlobalAlignmentKernelKMeans(KernelKMeans):
    def __init__(self, **kwargs):
        warnings.warn(
            "`GlobalAlignmentKernelKMeans` is deprecated in version "
            "0.4 and will be removed in 0.6. Use `KernelKMeans` "
            "instead.",
            DeprecationWarning, stacklevel=2)
        super().__init__(**kwargs)
        self.kernel = "gak"


class TimeSeriesCentroidBasedClusteringMixin:
    """Mixin class for centroid-based clustering of time series."""
    def _post_fit(self, X_fitted, centroids, inertia):
        if numpy.isfinite(inertia) and (centroids is not None):
            self.cluster_centers_ = centroids
            self._assign(X_fitted)
            self._X_fit = X_fitted
            self.inertia_ = inertia
        else:
            self._X_fit = None


class TimeSeriesKMeans(TransformerMixin, ClusterMixin,
                       TimeSeriesCentroidBasedClusteringMixin,
                       BaseModelPackage, TimeSeriesBaseEstimator):
    """K-means clustering for time-series data.

    Parameters
    ----------
    n_clusters : int (default: 3)
        Number of clusters to form.

    max_iter : int (default: 50)
        Maximum number of iterations of the k-means algorithm for a single run.

    tol : float (default: 1e-6)
        Inertia variation threshold. If at some point, inertia varies less than
        this threshold between two consecutive
        iterations, the model is considered to have converged and the algorithm
        stops.

    n_init : int (default: 1)
        Number of time the k-means algorithm will be run with different
        centroid seeds. The final results will be the best output of n_init
        consecutive runs in terms of inertia.

    metric : {"euclidean", "dtw", "softdtw"} (default: "euclidean")
        Metric to be used for both cluster assignment and barycenter
        computation. If "dtw", DBA is used for barycenter
        computation.

    max_iter_barycenter : int (default: 100)
        Number of iterations for the barycenter computation process. Only used
        if `metric="dtw"` or `metric="softdtw"`.

    metric_params : dict or None (default: None)
        Parameter values for the chosen metric.
        For metrics that accept parallelization of the cross-distance matrix
        computations, `n_jobs` key passed in `metric_params` is overridden by
        the `n_jobs` argument.

    n_jobs : int or None, optional (default=None)
        The number of jobs to run in parallel for cross-distance matrix
        computations.
        Ignored if the cross-distance matrix cannot be computed using
        parallelization.
        ``None`` means 1 unless in a :obj:`joblib.parallel_backend` context.
        ``-1`` means using all processors. See scikit-learns'
        `Glossary <https://scikit-learn.org/stable/glossary.html#term-n-jobs>`_
        for more details.

    dtw_inertia: bool (default: False)
        Whether to compute DTW inertia even if DTW is not the chosen metric.

    verbose : int (default: 0)
        If nonzero, print information about the inertia while learning
        the model and joblib progress messages are printed.

    random_state : integer or numpy.RandomState, optional
        Generator used to initialize the centers. If an integer is given, it
        fixes the seed. Defaults to the global
        numpy random number generator.

    init : {'k-means++', 'random' or an ndarray} (default: 'k-means++')
        Method for initialization:
        'k-means++' : use k-means++ heuristic. See `scikit-learn's k_init_
        <https://github.com/scikit-learn/scikit-learn/blob/master/sklearn/\
        cluster/k_means_.py>`_ for more.
        'random': choose k observations (rows) at random from data for the
        initial centroids.
        If an ndarray is passed, it should be of shape (n_clusters, ts_size, d)
        and gives the initial centers.

    Attributes
    ----------
    labels_ : numpy.ndarray
        Labels of each point.

    cluster_centers_ : numpy.ndarray of shape (n_clusters, sz, d)
        Cluster centers.
        `sz` is the size of the time series used at fit time if the init method
        is 'k-means++' or 'random', and the size of the longest initial
        centroid if those are provided as a numpy array through init parameter.

    inertia_ : float
        Sum of distances of samples to their closest cluster center.

    n_iter_ : int
        The number of iterations performed during fit.

    Notes
    -----
        If `metric` is set to `"euclidean"`, the algorithm expects a dataset of
        equal-sized time series.

    Examples
    --------
    >>> from tslearn.generators import random_walks
    >>> X = random_walks(n_ts=50, sz=32, d=1)
    >>> km = TimeSeriesKMeans(n_clusters=3, metric="euclidean", max_iter=5,
    ...                       random_state=0).fit(X)
    >>> km.cluster_centers_.shape
    (3, 32, 1)
    >>> km_dba = TimeSeriesKMeans(n_clusters=3, metric="dtw", max_iter=5,
    ...                           max_iter_barycenter=5,
    ...                           random_state=0).fit(X)
    >>> km_dba.cluster_centers_.shape
    (3, 32, 1)
    >>> km_sdtw = TimeSeriesKMeans(n_clusters=3, metric="softdtw", max_iter=5,
    ...                            max_iter_barycenter=5,
    ...                            metric_params={"gamma": .5},
    ...                            random_state=0).fit(X)
    >>> km_sdtw.cluster_centers_.shape
    (3, 32, 1)
    >>> X_bis = to_time_series_dataset([[1, 2, 3, 4],
    ...                                 [1, 2, 3],
    ...                                 [2, 5, 6, 7, 8, 9]])
    >>> km = TimeSeriesKMeans(n_clusters=2, max_iter=5,
    ...                       metric="dtw", random_state=0).fit(X_bis)
    >>> km.cluster_centers_.shape
    (2, 6, 1)
    """

    def __init__(self, n_clusters=3, max_iter=50, tol=1e-6, n_init=1,
                 metric="euclidean", max_iter_barycenter=100,
                 metric_params=None, n_jobs=None, dtw_inertia=False,
                 verbose=0, random_state=None, init='k-means++'):
        self.n_clusters = n_clusters
        self.max_iter = max_iter
        self.tol = tol
        self.n_init = n_init
        self.metric = metric
        self.max_iter_barycenter = max_iter_barycenter
        self.metric_params = metric_params
        self.n_jobs = n_jobs
        self.dtw_inertia = dtw_inertia
        self.verbose = verbose
        self.random_state = random_state
        self.init = init

    def _is_fitted(self):
        check_is_fitted(self, ['cluster_centers_'])
        return True

    def _get_metric_params(self):
        if self.metric_params is None:
            metric_params = {}
        else:
            metric_params = self.metric_params.copy()
        if "n_jobs" in metric_params.keys():
            del metric_params["n_jobs"]
        return metric_params

    def _fit_one_init(self, X, x_squared_norms, rs):
        metric_params = self._get_metric_params()
        n_ts, sz, d = X.shape
        if hasattr(self.init, '__array__'):
            self.cluster_centers_ = self.init.copy()
        elif self.init == "k-means++":
            if self.metric == "euclidean":
                self.cluster_centers_ = _k_init(
                    X.reshape((n_ts, -1)),
                    self.n_clusters,
                    x_squared_norms,
                    rs
                ).reshape((-1, sz, d))
            else:
                if self.metric == "dtw":
                    def metric_fun(x, y):
                        return cdist_dtw(x, y, n_jobs=self.n_jobs,
                                         verbose=self.verbose, **metric_params)

                elif self.metric == "softdtw":
                    def metric_fun(x, y):
                        return cdist_soft_dtw(x, y, **metric_params)
                else:
                    raise ValueError(
                        "Incorrect metric: %s (should be one of 'dtw', "
                        "'softdtw', 'euclidean')" % self.metric
                    )
                self.cluster_centers_ = _k_init_metric(X, self.n_clusters,
                                                       cdist_metric=metric_fun,
                                                       random_state=rs)
        elif self.init == "random":
            indices = rs.choice(X.shape[0], self.n_clusters)
            self.cluster_centers_ = X[indices].copy()
        else:
            raise ValueError("Value %r for parameter 'init'"
                             "is invalid" % self.init)
        self.cluster_centers_ = _check_full_length(self.cluster_centers_)
        old_inertia = numpy.inf

        for it in range(self.max_iter):
            self._assign(X)
            if self.verbose:
                print("%.3f" % self.inertia_, end=" --> ")
            self._update_centroids(X)

            if numpy.abs(old_inertia - self.inertia_) < self.tol:
                break
            old_inertia = self.inertia_
        if self.verbose:
            print("")

        self._iter = it + 1

        return self

    def _transform(self, X):
        metric_params = self._get_metric_params()
        if self.metric == "euclidean":
            return cdist(X.reshape((X.shape[0], -1)),
                          self.cluster_centers_.reshape((self.n_clusters, -1)),
                          metric="euclidean")
        elif self.metric == "dtw":
            return cdist_dtw(X, self.cluster_centers_, n_jobs=self.n_jobs,
                              verbose=self.verbose, **metric_params)
        elif self.metric == "softdtw":
            return cdist_soft_dtw(X, self.cluster_centers_, **metric_params)
        else:
            raise ValueError("Incorrect metric: %s (should be one of 'dtw', "
                             "'softdtw', 'euclidean')" % self.metric)

    def _assign(self, X, update_class_attributes=True):
        dists = self._transform(X)
        matched_labels = dists.argmin(axis=1)
        if update_class_attributes:
            self.labels_ = matched_labels
            _check_no_empty_cluster(self.labels_, self.n_clusters)
            if self.dtw_inertia and self.metric != "dtw":
                inertia_dists = cdist_dtw(X, self.cluster_centers_,
                                          n_jobs=self.n_jobs,
                                          verbose=self.verbose)
            else:
                inertia_dists = dists
            self.inertia_ = _compute_inertia(inertia_dists,
                                             self.labels_,
                                             self._squared_inertia)
        return matched_labels

    def _update_centroids(self, X):
        metric_params = self._get_metric_params()
        for k in range(self.n_clusters):
            if self.metric == "dtw":
                self.cluster_centers_[k] = dtw_barycenter_averaging(
                    X=X[self.labels_ == k],
                    barycenter_size=None,
                    init_barycenter=self.cluster_centers_[k],
                    metric_params=metric_params,
                    verbose=False)
            elif self.metric == "softdtw":
                self.cluster_centers_[k] = softdtw_barycenter(
                    X=X[self.labels_ == k],
                    max_iter=self.max_iter_barycenter,
                    init=self.cluster_centers_[k],
                    **metric_params)
            else:
                self.cluster_centers_[k] = euclidean_barycenter(
                    X=X[self.labels_ == k])

    def fit(self, X, y=None):
        """Compute k-means clustering.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset.

        y
            Ignored
        """

        X = check_array(X, allow_nd=True, force_all_finite='allow-nan')

        if hasattr(self.init, '__array__'):
            X = check_dims(X, X_fit_dims=self.init.shape,
                           extend=True,
                           check_n_features_only=(self.metric != "euclidean"))


        self.labels_ = None
        self.inertia_ = numpy.inf
        self.cluster_centers_ = None
        self._X_fit = None
        self._squared_inertia = True

        self.n_iter_ = 0

        max_attempts = max(self.n_init, 10)

        X_ = to_time_series_dataset(X)
        rs = check_random_state(self.random_state)

        if self.init == "k-means++" and self.metric == "euclidean":
            n_ts, sz, d = X_.shape
            x_squared_norms = cdist(X_.reshape((n_ts, -1)),
                                    numpy.zeros((1, sz * d)),
                                    metric="sqeuclidean").reshape((1, -1))
        else:
            x_squared_norms = None
        _check_initial_guess(self.init, self.n_clusters)

        best_correct_centroids = None
        min_inertia = numpy.inf
        n_successful = 0
        n_attempts = 0
        while n_successful < self.n_init and n_attempts < max_attempts:
            try:
                if self.verbose and self.n_init > 1:
                    print("Init %d" % (n_successful + 1))
                n_attempts += 1
                self._fit_one_init(X_, x_squared_norms, rs)
                if self.inertia_ < min_inertia:
                    best_correct_centroids = self.cluster_centers_.copy()
                    min_inertia = self.inertia_
                    self.n_iter_ = self._iter
                n_successful += 1
            except EmptyClusterError:
                if self.verbose:
                    print("Resumed because of empty cluster")
        self._post_fit(X_, best_correct_centroids, min_inertia)
        return self

    def fit_predict(self, X, y=None):
        """Fit k-means clustering using X and then predict the closest cluster
        each time series in X belongs to.

        It is more efficient to use this method than to sequentially call fit
        and predict.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset to predict.

        y
            Ignored

        Returns
        -------
        labels : array of shape=(n_ts, )
            Index of the cluster each sample belongs to.
        """
        X = check_array(X, allow_nd=True, force_all_finite='allow-nan')
        return self.fit(X, y).labels_

    def predict(self, X):
        """Predict the closest cluster each time series in X belongs to.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset to predict.

        Returns
        -------
        labels : array of shape=(n_ts, )
            Index of the cluster each sample belongs to.
        """
        X = check_array(X, allow_nd=True, force_all_finite='allow-nan')
        check_is_fitted(self, 'cluster_centers_')
        X = check_dims(X, X_fit_dims=self.cluster_centers_.shape,
                       extend=True,
                       check_n_features_only=(self.metric != "euclidean"))
        return self._assign(X, update_class_attributes=False)

    def transform(self, X):
        """Transform X to a cluster-distance space.

        In the new space, each dimension is the distance to the cluster
        centers.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset

        Returns
        -------
        distances : array of shape=(n_ts, n_clusters)
            Distances to cluster centers
        """
        X = check_array(X, allow_nd=True, force_all_finite='allow-nan')
        check_is_fitted(self, 'cluster_centers_')
        X = check_dims(X, X_fit_dims=self.cluster_centers_.shape,
                       extend=True,
                       check_n_features_only=(self.metric != "euclidean"))
        return self._transform(X)

    def _more_tags(self):
        return {'allow_nan': True, 'allow_variable_length': True}


class KShape(ClusterMixin, TimeSeriesCentroidBasedClusteringMixin,
             BaseModelPackage, TimeSeriesBaseEstimator):
    """KShape clustering for time series.

    KShape was originally presented in [1]_.

    Parameters
    ----------
    n_clusters : int (default: 3)
        Number of clusters to form.

    max_iter : int (default: 100)
        Maximum number of iterations of the k-Shape algorithm.

    tol : float (default: 1e-6)
        Inertia variation threshold. If at some point, inertia varies less than
        this threshold between two consecutive
        iterations, the model is considered to have converged and the algorithm
        stops.

    n_init : int (default: 1)
        Number of time the k-Shape algorithm will be run with different
        centroid seeds. The final results will be the
        best output of n_init consecutive runs in terms of inertia.

    verbose : bool (default: False)
        Whether or not to print information about the inertia while learning
        the model.

    random_state : integer or numpy.RandomState, optional
        Generator used to initialize the centers. If an integer is given, it
        fixes the seed. Defaults to the global
        numpy random number generator.

    init : {'random' or ndarray} (default: 'random')
        Method for initialization.
        'random': choose k observations (rows) at random from data for the
        initial centroids.
        If an ndarray is passed, it should be of shape (n_clusters, ts_size, d)
        and gives the initial centers.

    Attributes
    ----------
    cluster_centers_ : numpy.ndarray of shape (sz, d).
        Centroids

    labels_ : numpy.ndarray of integers with shape (n_ts, ).
        Labels of each point

    inertia_ : float
        Sum of distances of samples to their closest cluster center.

    n_iter_ : int
        The number of iterations performed during fit.

    Notes
    -----
        This method requires a dataset of equal-sized time series.

    Examples
    --------
    >>> from tslearn.generators import random_walks
    >>> X = random_walks(n_ts=50, sz=32, d=1)
    >>> X = TimeSeriesScalerMeanVariance(mu=0., std=1.).fit_transform(X)
    >>> ks = KShape(n_clusters=3, n_init=1, random_state=0).fit(X)
    >>> ks.cluster_centers_.shape
    (3, 32, 1)

    References
    ----------
    .. [1] J. Paparrizos & L. Gravano. k-Shape: Efficient and Accurate
       Clustering of Time Series. SIGMOD 2015. pp. 1855-1870.
    """

    def __init__(self, n_clusters=3, max_iter=100, tol=1e-6, n_init=1,
                 verbose=False, random_state=None, init='random',n_component=1,mode="max"):
        self.n_clusters = n_clusters
        self.max_iter = max_iter
        self.tol = tol
        self.random_state = random_state
        self.n_init = n_init
        self.verbose = verbose
        self.init = init
        self.n_component = n_component
        self.mode = mode
    def _is_fitted(self):
        """
        Check if the model has been fit.

        Returns
        -------
        bool
        """

        check_is_fitted(self,
                        ['cluster_centers_', 'norms_', 'norms_centroids_'])
        return True

    def _shape_extraction(self, X, k):
        sz = X.shape[1]
        Xp = y_shifted_sbd_vec(self.cluster_centers_[k], X[self.labels_ == k],
                               norm_ref=-1, norms_dataset=self.norms_[self.labels_ == k],
                               n_component=self.n_component,mode=self.mode)
        S = numpy.dot(Xp[:, :, 0].T, Xp[:, :, 0])
        Q = numpy.eye(sz) - numpy.ones((sz, sz)) / sz
        M = numpy.dot(Q.T, numpy.dot(S, Q))
        _, vec = numpy.linalg.eigh(M)
        mu_k = vec[:, -1].reshape((sz, 1))

        # The way the optimization problem is (ill-)formulated, both mu_k and
        # -mu_k are candidates for barycenters
        # In the following, we check which one is best candidate
        dist_plus_mu = numpy.sum(numpy.linalg.norm(Xp - mu_k, axis=(1, 2)))
        dist_minus_mu = numpy.sum(numpy.linalg.norm(Xp + mu_k, axis=(1, 2)))
        if dist_minus_mu < dist_plus_mu:
            mu_k *= -1

        return mu_k

    def _update_centroids(self, X):
        for k in range(self.n_clusters):
            self.cluster_centers_[k] = self._shape_extraction(X, k)
        self.cluster_centers_ = TimeSeriesScalerMeanVariance(
            mu=0., std=1.).fit_transform(self.cluster_centers_)
        self.norms_centroids_ = numpy.linalg.norm(self.cluster_centers_,
                                                  axis=(1, 2))

    def _cross_dists(self, X):
        return 1. - cdist_normalized_cc(X, self.cluster_centers_,
                                        norms1=self.norms_,
                                        norms2=self.norms_centroids_,
                                        self_similarity=False,n_component=self.n_component,mode=self.mode)

    def _assign(self, X):
        dists = self._cross_dists(X)
        self.labels_ = dists.argmin(axis=1)
        _check_no_empty_cluster(self.labels_, self.n_clusters)
        self.inertia_ = _compute_inertia(dists, self.labels_)

    def _fit_one_init(self, X, rs):
        if hasattr(self.init, '__array__'):
            self.cluster_centers_ = self.init.copy()
        elif self.init == "random":
            indices = rs.choice(X.shape[0], self.n_clusters)
            self.cluster_centers_ = X[indices].copy()
        else:
            raise ValueError("Value %r for parameter 'init' is "
                             "invalid" % self.init)
        self.norms_centroids_ = numpy.linalg.norm(self.cluster_centers_,
                                                  axis=(1, 2))
        self._assign(X)
        old_inertia = numpy.inf

        for it in range(self.max_iter):
            old_cluster_centers = self.cluster_centers_.copy()
            self._update_centroids(X)
            self._assign(X)
            if self.verbose:
                print("%.3f" % self.inertia_, end=" --> ")

            if numpy.abs(old_inertia - self.inertia_) < self.tol or \
                    (old_inertia - self.inertia_ < 0):
                self.cluster_centers_ = old_cluster_centers
                self._assign(X)
                break

            old_inertia = self.inertia_
        if self.verbose:
            print("")

        self._iter = it + 1

        return self

    def fit(self, X, y=None):
        """Compute k-Shape clustering.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset.

        y
            Ignored
        """
        X = check_array(X, allow_nd=True)

        max_attempts = max(self.n_init, 10)

        self.labels_ = None
        self.inertia_ = numpy.inf
        self.cluster_centers_ = None

        self.norms_ = 0.
        self.norms_centroids_ = 0.

        self.n_iter_ = 0

        X_ = to_time_series_dataset(X)
        self._X_fit = X_
        self.norms_ = numpy.linalg.norm(X_, axis=(1, 2))

        _check_initial_guess(self.init, self.n_clusters)

        rs = check_random_state(self.random_state)

        best_correct_centroids = None
        min_inertia = numpy.inf
        n_successful = 0
        n_attempts = 0
        while n_successful < self.n_init and n_attempts < max_attempts:
            try:
                if self.verbose and self.n_init > 1:
                    print("Init %d" % (n_successful + 1))
                n_attempts += 1
                self._fit_one_init(X_, rs)
                if self.inertia_ < min_inertia:
                    best_correct_centroids = self.cluster_centers_.copy()
                    min_inertia = self.inertia_
                    self.n_iter_ = self._iter
                n_successful += 1
            except EmptyClusterError:
                if self.verbose:
                    print("Resumed because of empty cluster")
        self.norms_centroids_ = numpy.linalg.norm(self.cluster_centers_,
                                                  axis=(1, 2))
        self._post_fit(X_, best_correct_centroids, min_inertia)
        return self

    def fit_predict(self, X, y=None):
        """Fit k-Shape clustering using X and then predict the closest cluster
        each time series in X belongs to.

        It is more efficient to use this method than to sequentially call fit
        and predict.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset to predict.

        y
            Ignored

        Returns
        -------
        labels : array of shape=(n_ts, )
            Index of the cluster each sample belongs to.
        """
        return self.fit(X, y).labels_

    def predict(self, X):
        """Predict the closest cluster each time series in X belongs to.

        Parameters
        ----------
        X : array-like of shape=(n_ts, sz, d)
            Time series dataset to predict.

        Returns
        -------
        labels : array of shape=(n_ts, )
            Index of the cluster each sample belongs to.
        """
        X = check_array(X, allow_nd=True)
        check_is_fitted(self,
                        ['cluster_centers_', 'norms_', 'norms_centroids_'])

        X_ = check_dims(X, X_fit_dims=self.cluster_centers_.shape)
        X_ = TimeSeriesScalerMeanVariance(mu=0., std=1.).fit_transform(X_)
        dists = self._cross_dists(X_)
        return dists.argmin(axis=1)
