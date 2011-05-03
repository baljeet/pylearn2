"""Autoencoders, denoising autoencoders, and stacked DAEs."""
# Standard library imports
import functools
from itertools import izip

# Third-party imports
import numpy
import theano
from theano import tensor

# Local imports
from .base import Block, StackedBlocks
from .scalar import rectifier
from .utils import sharedX
from .utils.theano_graph import is_pure_elemwise

theano.config.warn.sum_div_dimshuffle_bug = False
floatX = theano.config.floatX

if 0:
    print 'WARNING: using SLOW rng'
    RandomStreams = tensor.shared_randomstreams.RandomStreams
else:
    import theano.sandbox.rng_mrg
    RandomStreams = theano.sandbox.rng_mrg.MRG_RandomStreams


class Autoencoder(Block):
    """
    Base class implementing ordinary autoencoders.

    More exotic variants (denoising, contracting autoencoders) can inherit
    much of the necessary functionality and override what they need.
    """
    def __init__(self, nvis, nhid, act_enc, act_dec,
                 tied_weights=False, solution='', sparse_penalty=0,
                 sparsity_target=0, sparsity_target_penalty=0,
                 irange=1e-3, rng=9001):
        """
        Allocate an autoencoder object.

        Parameters
        ----------
        nvis : int
            Number of visible units (input dimensions) in this model.
            A value of 0 indicates that this block will be left partially
            initialized until later (e.g., when the dataset is loaded and
            its dimensionality is known)
        nhid : int
            Number of hidden units in this model.
        act_enc : callable or string
            Activation function (elementwise nonlinearity) to use for the
            encoder. Strings (e.g. 'tanh' or 'sigmoid') will be looked up as
            functions in `theano.tensor.nnet` and `theano.tensor`. Use `None`
            for linear units.
        act_dec : callable or string
            Activation function (elementwise nonlinearity) to use for the
            decoder. Strings (e.g. 'tanh' or 'sigmoid') will be looked up as
            functions in `theano.tensor.nnet` and `theano.tensor`. Use `None`
            for linear units.
        tied_weights : bool, optional
            If `False` (default), a separate set of weights will be allocated
            (and learned) for the encoder and the decoder function. If `True`,
            the decoder weight matrix will be constrained to be equal to the
            transpose of the encoder weight matrix.
        solution : string
            If empty (default), the regularization term for the cost will be 0.
            If 'l1_penalty', add to loss a L1 penalty.
            If 'sqr_penalty', add to loss a quadratic penalty
        sparse_penalty : float, optional
            hyperparameter to control the value of the regularization term for
            the L1 penalty.
        sparsity_target : float, optional
            hyperparameter to control the value of the regularization term for
            the L2 penalty.
        sparsity_target_penalty : float, optional
            hyperparameter to control difference between the values of
            hiddens output of the regularization term for the quadratic penalty
        irange : float, optional
            Width of the initial range around 0 from which to sample initial
            values for the weights.
        rng : RandomState object or seed
            NumPy random number generator object (or seed to create one) used
            to initialize the model parameters.
        """
        assert nvis >= 0, "Number of visible units must be non-negative"
        assert nhid > 0, "Number of hidden units must be positive"
        # Save a few parameters needed for resizing
        self.nhid = nhid
        self.irange = irange
        self.tied_weights = tied_weights
        if not hasattr(rng, 'randn'):
            self.rng = numpy.random.RandomState(rng)
        else:
            self.rng = rng
        self._initialize_hidbias()
        if nvis > 0:
            self._initialize_visbias(nvis)
            self._initialize_weights(nvis)
        else:
            self.visbias = None
            self.weights = None

        seed = int(self.rng.randint(2 ** 30))
        self.s_rng = RandomStreams(seed)
        if tied_weights:
            self.w_prime = self.weights.T
        else:
            self._initialize_w_prime(nvis)

        def _resolve_callable(conf, conf_attr):
            if conf[conf_attr] is None or conf[conf_attr] == "linear":
                return None
            # If it's a callable, use it directly.
            if hasattr(conf[conf_attr], '__call__'):
                return conf[conf_attr]
            elif (conf[conf_attr] in globals()
                  and hasattr(globals()[conf[conf_attr]], '__call__')):
                return globals()[conf[conf_attr]]
            elif hasattr(tensor.nnet, conf[conf_attr]):
                return getattr(tensor.nnet, conf[conf_attr])
            elif hasattr(tensor, conf[conf_attr]):
                return getattr(tensor, conf[conf_attr])
            else:
                raise ValueError("Couldn't interpret %s value: '%s'" %
                                    (conf_attr, conf[conf_attr]))

        self.act_enc = _resolve_callable(locals(), 'act_enc')
        self.act_dec = _resolve_callable(locals(), 'act_dec')
        self._params = [
            self.visbias,
            self.hidbias,
            self.weights,
        ]
        if not self.tied_weights:
            self._params.append(self.w_prime)

        self.solution = solution
        self.sparse_penalty = sparse_penalty
        self.sparsity_target = sparsity_target
        self.sparsity_target_penalty = sparsity_target_penalty
        self.regularization = 0

    def _initialize_weights(self, nvis, rng=None, irange=None):
        if rng is None:
            rng = self.rng
        if irange is None:
            irange = self.irange
        # TODO: use weight scaling factor if provided, Xavier's default else
        self.weights = sharedX(
            .5 - rng.rand(nvis, self.nhid) * self.irange,
            name='W',
            borrow=True
        )

    def _initialize_hidbias(self):
        self.hidbias = sharedX(
            numpy.zeros(self.nhid),
            name='hb',
            borrow=True
        )

    def _initialize_visbias(self, nvis):
        self.visbias = sharedX(
            numpy.zeros(nvis),
            name='vb',
            borrow=True
        )

    def _initialize_w_prime(self, nvis, rng=None, irange=None):
        assert (not self.tied_weights,
            "Can't initialize w_prime in tied weights model; "
            "this method shouldn't have been called")
        if rng is None:
            rng = self.rng
        if irange is None:
            irange = self.irange
        self.w_prime = sharedX(
            .5 - rng.rand(self.nhid, nvis) * irange,
            name='Wprime',
            borrow=True
        )

    def set_visible_size(self, nvis, rng=None):
        """
        Create and initialize the necessary parameters to accept
        `nvis` sized inputs.

        Parameters
        ----------
        nvis : int
            Number of visible units for the model.
        rng : RandomState object or seed, optional
            NumPy random number generator object (or seed to create one) used
            to initialize the model parameters. If not provided, the stored
            rng object (from the time of construction) will be used.
        """
        if self.weights is not None:
            raise ValueError('parameters of this model already initialized; '
                             'create a new object instead')
        if rng is not None:
            self.rng = rng
        else:
            rng = self.rng
        self._initialize_visbias(nvis)
        self._initialize_weights(nvis, rng)
        if not self.tied_weights:
            self._initialize_w_prime(nvis, rng)
        self._set_params()

    def _hidden_activation(self, x):
        """
        Single minibatch activation function.

        Parameters
        ----------
        x : tensor_like
            Theano symbolic representing the input minibatch.

        Returns
        -------
        y : tensor_like
            (Symbolic) hidden unit activations given the input.
        """
        if self.act_enc is None:
            act_enc = lambda x: x
        else:
            act_enc = self.act_enc
        return act_enc(self._hidden_input(x))

    def _hidden_input(self, x):
        """
        Given a single minibatch, computes the input to the
        activation nonlinearity without applying it.

        Parameters
        ----------
        x : tensor_like
            Theano symbolic representing the input minibatch.

        Returns
        -------
        y : tensor_like
            (Symbolic) input flowing into the hidden layer nonlinearity.
        """
        return self.hidbias + tensor.dot(x, self.weights)

    def encode(self, inputs):
        """
        Map inputs through the encoder function.

        Parameters
        ----------
        inputs : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the input
            minibatch(es) to be encoded. Assumed to be 2-tensors, with the
            first dimension indexing training examples and the second indexing
            data dimensions.

        Returns
        -------
        encoded : tensor_like or list of tensor_like
            Theano symbolic (or list thereof) representing the corresponding
            reconstructed minibatch(es) after encoding/decoding.
        """
        if isinstance(inputs, tensor.Variable):
            return self._hidden_activation(inputs)
        else:
            return [self.encode(v) for v in inputs]

    def reconstruct(self, inputs):
        """
        Reconstruct (decode) the inputs after mapping through the encoder.

        Parameters
        ----------
        inputs : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the input
            minibatch(es) to be encoded and reconstructed. Assumed to be
            2-tensors, with the first dimension indexing training examples and
            the second indexing data dimensions.

        Returns
        -------
        reconstructed : tensor_like or list of tensor_like
            Theano symbolic (or list thereof) representing the corresponding
            reconstructed minibatch(es) after encoding/decoding.
        """
        hiddens = self.encode(inputs)
        self.hiddens = hiddens
        self.regularization = self.compute_regularization(hiddens)

        if self.act_dec is None:
            act_dec = lambda x: x
        else:
            act_dec = self.act_dec
        if isinstance(inputs, tensor.Variable):
            return act_dec(self.visbias + tensor.dot(hiddens, self.w_prime))
        else:
            return [self.reconstruct(inp) for inp in inputs]

    def compute_penalty_value(self):
        '''
        Return the penalty value compute by the function compute_regularization
        '''
        return self.regularization

    def compute_regularization(self, hiddens):
        """
        Compute the penalty value depending on the choice solution (L1 or L2).
        """
        regularization = 0
        # Compute regularization term
        if self.solution == 'l1_penalty':
            regularization = self.sparse_penalty * tensor.sum(hiddens)
        elif self.solution == 'sqr_penalty':
            regularization = (self.sparsity_target_penalty
                              * tensor.sum(tensor.sqr(hiddens
                                                      - self.sparsity_target)))

        return regularization

    def __call__(self, inputs):
        """
        Forward propagate (symbolic) input through this module, obtaining
        a representation to pass on to layers above.

        This just aliases the `encode()` function for syntactic
        sugar/convenience.
        """
        return self.encode(inputs)


class DenoisingAutoencoder(Autoencoder):
    """
    A denoising autoencoder learns a representation of the input by
    reconstructing a noisy version of it.
    """
    def __init__(self, corruptor, nvis, nhid, act_enc, act_dec,
                 tied_weights=False, solution='', sparse_penalty=0,
                 sparsity_target=0, sparsity_target_penalty=0,
                 irange=1e-3, rng=9001):
        """
        Allocate a denoising autoencoder object.

        Parameters
        ----------
        corruptor : object
            Instance of a corruptor object to use for corrupting the
            input.

        Notes
        -----
        The remaining parameters are identical to those of the constructor
        for the Autoencoder class; see the `Autoencoder.__init__` docstring
        for details.
        """
        super(DenoisingAutoencoder, self).__init__(
            nvis,
            nhid,
            act_enc,
            act_dec,
            tied_weights,
            solution,
            sparse_penalty,
            sparsity_target,
            sparsity_target_penalty,
            irange,
            rng
        )
        self.corruptor = corruptor

    def reconstruct(self, inputs):
        """
        Reconstruct the inputs after corrupting and mapping through the
        encoder and decoder.

        Parameters
        ----------
        inputs : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the input
            minibatch(es) to be corrupted and reconstructed. Assumed to be
            2-tensors, with the first dimension indexing training examples and
            the second indexing data dimensions.

        Returns
        -------
        reconstructed : tensor_like or list of tensor_like
            Theano symbolic (or list thereof) representing the corresponding
            reconstructed minibatch(es) after corruption and encoding/decoding.
        """
        corrupted = self.corruptor(inputs)
        return super(DenoisingAutoencoder, self).reconstruct(corrupted)


class ContractingAutoencoder(Autoencoder):
    """
    A contracting autoencoder works like a regular autoencoder, and adds an
    extra term to its cost function.
    """
    @functools.wraps(Autoencoder.__init__)
    def __init__(self, *args, **kwargs):
        super(ContractingAutoencoder, self).__init__(*args, **kwargs)
        dummyinput = tensor.matrix()
        if not is_pure_elemwise(self.act_enc(dummyinput), [dummyinput]):
            raise ValueError("Invalid encoder activation function: "
                             "not an elementwise function of its input")

    def contraction_penalty(self, inputs):
        """
        Calculate (symbolically) the contracting autoencoder penalty term.

        Parameters
        ----------
        inputs : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the input
            minibatch(es) on which the penalty is calculated. Assumed to be
            2-tensors, with the first dimension indexing training examples and
            the second indexing data dimensions.

        Returns
        -------
        penalty : tensor_like
            0-dimensional tensor (i.e. scalar) that penalizes the Jacobian
            matrix of the encoder transformation. Add this to the output
            of a Cost object such as MeanSquaredError to penalize it.

        Notes
        -----
        Theano's differentiation capabilities do not currently allow
        (efficient) automatic evaluation of the Jacobian, mainly because
        of the immature state of the `scan` operator. Here we use a
        "semi-automatic" hack that works for hidden layers of the for
        :math:`s(Wx + b)`, where `s` is the activation function, :math:`W`
        is `self.weights`, and :math:`b` is `self.hidbias`, by only taking
        the derivative of :math:`s` with respect :math:`a = Wx + b` and
        manually constructing the Jacobian from there.

        Because of this implementation depends *critically* on the
        _hidden_inputs() method implementing only an affine transformation
        by the weights (i.e. :math:`Wx + b`), and the activation function
        `self.act_enc` applying an independent, elementwise operation.
        """
        def penalty(inputs):
            # Compute the input flowing into the hidden units, i.e. the
            # value before applying the nonlinearity/activation function
            acts = self._hidden_input(inputs)
            # Apply the activating nonlinearity.
            hiddens = self.act_enc(acts)
            # We want dh/da for every pre/postsynaptic pair, which we
            # can easily do by taking the gradient of the sum of the
            # hidden units activations w.r.t the presynaptic activity,
            # since the gradient of hiddens.sum() with respect to hiddens
            # is a matrix of ones!
            act_grad = tensor.grad(hiddens.sum(), acts)
            # As long as act_enc is an elementwise operator, the Jacobian
            # of a act_enc(Wx + b) hidden layer has a Jacobian of the
            # following form.
            jacobian = self.weights * act_grad.dimshuffle(0, 'x', 1)
            # Penalize the mean of the L2 norm, basically.
            L = tensor.sum(jacobian ** 2)
            return L
        if isinstance(inputs, tensor.Variable):
            return penalty(inputs)
        else:
            return [penalty(inp) for inp in inputs]


def build_stacked_ae(nvis, nhids, act_enc, act_dec,
                     tied_weights=False, irange=1e-3, rng=None,
                     corruptor=None, contracting=False,
                     solution=None, sparse_penalty=None,
                     sparsity_target=None, sparsity_target_penalty=None):
    """Allocate a stack of autoencoders."""
    if not hasattr(rng, 'randn'):
        rng = numpy.random.RandomState(rng)
    layers = []
    final = {}
    # "Broadcast" arguments if they are singular, or accept sequences if
    # they are the same length as nhids
    for c in ['corruptor', 'contracting', 'act_enc', 'act_dec',
              'tied_weights', 'irange', 'solution', 'sparse_penalty',
              'sparsity_target', 'sparsity_target_penalty']:
        if type(locals()[c]) is not str and hasattr(locals()[c], '__len__'):
            assert len(nhids) == len(locals()[c])
            final[c] = locals()[c]
        else:
            final[c] = [locals()[c]] * len(nhids)
    # The number of visible units in each layer is the initial input
    # size and the first k-1 hidden unit sizes.
    # solution, sparse_penalty, sparsity_target,
    # and sparsity_target_penalty have the same size as nhids.
    # They can add an L1 penalty, a quadratic to each layer of the stacked ae.
    nviss = [nvis] + nhids[:-1]
    seq = izip(nhids, nviss,
        final['act_enc'],
        final['act_dec'],
        final['corruptor'],
        final['contracting'],
        final['tied_weights'],
        final['irange'],
        final['solution'],
        final['sparse_penalty'],
        final['sparsity_target'],
        final['sparsity_target_penalty']
    )
    # Create each layer.
    for (nhid, nvis, act_enc, act_dec, corr, cae, tied, ir,
         sol, spar_pen, spar_tar, spar_tar_pen) in seq:
        args = (nvis, nhid, act_enc, act_dec, tied, sol,
                spar_pen, spar_tar, spar_tar_pen, ir, rng)
        if cae and corr is not None:
            raise ValueError("Can't specify denoising and contracting "
                             "objectives simultaneously")
        elif cae:
            autoenc = ContractingAutoencoder(*args)
        elif corr is not None:
            autoenc = DenoisingAutoencoder(corr, *args)
        else:
            autoenc = Autoencoder(*args)
        layers.append(autoenc)

    # Create the stack
    return StackedBlocks(layers)


##################################################
def get(str):
    """ Evaluate str into an autoencoder object, if it exists """
    obj = globals()[str]
    if issubclass(obj, Autoencoder):
        return obj
    else:
        raise NameError(str)