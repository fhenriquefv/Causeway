import logging
import numpy as np

from pipeline.models import Model, FeaturizedModel
from pipeline.featurization import Featurizer


class StructuredModel(Model):
    '''
    In a structured model, every instance is divided up into "parts." Those
    parts are treated as the thing to be scored by the model. Thus, this class
    overrides the default train and test methods to extract parts first, and
    then call the normal test/train on the parts rather than the instances.
    (Thus, it's often a good idea for the parts to store pointers to the
    original instances for use in featurization, as the feature extractors won't
    get a copy of the original instance on the side.)

    A StructuredModel also has a StructuredDecoder, which is used to decode the
    scored parts into a coherent labeling for the instance.
    '''

    def __init__(self, decoder):
        ''' decoder is some StructuredDecoder object. '''
        self.decoder = decoder

    def train(self, instances):
        self.reset() # Reset state in case we've been previously trained.
        parts_by_instance = [self._make_parts(instance)
                             for instance in instances]
        self._train_structured(instances, parts_by_instance)

    # TODO: should structured models have Trainers, like in scikit-learn?
    # That would allow, e.g., training the same model with 2 different methods.
    def _train_structured(self, instances, parts_by_instance):
        raise NotImplementedError

    def test(self, instances):
        parts_by_instance = [self._make_parts(instance)
                             for instance in instances]
        outputs_by_instance = []
        for instance, instance_parts in zip(instances, parts_by_instance):
            part_scores = self._score_parts(instance, instance_parts)
            outputs_by_instance.append(
                self.decoder.decode(instance, instance_parts, part_scores))
        return outputs_by_instance

    def _make_parts(self, instance):
        raise NotImplementedError

    def _score_parts(self, instance_parts):
        raise NotImplementedError


class FeaturizedStructuredModel(StructuredModel, FeaturizedModel):
    '''
    If there is more than one part type, the subclass's _load_model() function
    should return a list of NameDictionary objects and/or selected features
    lists, one per part type (since each part type will be featurized
    separately.) self.featurizers will be populated in the order specified by
    these dictionaries.
    '''
    def __init__(self, decoder, part_types, selected_features=None,
                 part_filters=None, model_path=None, save_featurized=False):
        """
        decoder is some StructuredDecoder object.
        part_types is a list of types of part that will need to be featurized
            separately (e.g., node and edge parts). These should be the actual
            Python types of the parts returned by _make_parts.
        selected_features is a list of names of features to extract.
            Names may be conjoined by FLAGS.conjoined_feature_sep.
        part_filters is a list filter functions, corresponding to part_types,
            that take an part and return True iff it should be featurized. Parts
            that are filtered out will be featurized as all zeros.
        model_path is a path to a model to load. Either model_path or
            selected_features must be specified.
        save_featurized indicates whether to store features and labels
            properties after featurization. Useful for debugging/development.
        """
        if not part_filters:
            part_filters = [None] * len(part_types)
        else:
            assert len(part_filters) == len(part_types)
        self._part_filters = part_filters

        StructuredModel.__init__(self, decoder)
        FeaturizedModel.__init__(self, selected_features, model_path,
                                 save_featurized)
        if not model_path: # load() won't be called by FeaturizedModel
            self._set_up_featurizers(selected_features)
        self.part_types = part_types

    def _set_up_featurizers(self, selected_features_or_name_dicts):
        self.featurizers = []
        for featurizer_params, part_filter in zip(
            selected_features_or_name_dicts, self._part_filters):
            featurizer = Featurizer(
                self.all_feature_extractors, featurizer_params, part_filter,
                self.save_featurized)
            self.featurizers.append(featurizer)

    def _post_model_load(self, feature_name_dictionaries):
        super(FeaturizedStructuredModel, self)._post_model_load(
            feature_name_dictionaries)
        self._set_up_featurizers(feature_name_dictionaries)

    def reset(self):
        super(FeaturizedStructuredModel, self).reset()
        for featurizer in self.featurizers:
            featurizer.reset()

    def _score_parts(self, instance, instance_parts):
        featurized_parts_by_type = []
        for part_type, featurizer in zip(self.part_types, self.featurizers):
            relevant_parts = self._get_parts_of_type(part_type, instance_parts)
            featurized = featurizer.featurize(relevant_parts)
            featurized_parts_by_type.append(featurized)
        return self._score_featurized_parts(instance, featurized_parts_by_type)

    # Support function
    def _get_parts_of_type(self, part_type, parts):
        return [part for part in parts if isinstance(part, part_type)]

    def _score_featurized_parts(self, instance, featurized_parts_by_type):
        raise NotImplementedError


class StructuredDecoder(object):
    def decode(self, instance, instance_parts, scores):
        raise NotImplementedError


# TODO: move code below to separate sequences submodule?
class SequenceScores(object):
    def __init__(self, node_scores, transition_weights):
        self.node_scores = node_scores
        self.transition_weights = transition_weights


class Semiring(object):
    def __init__(self, np_sum, np_multiply, additive_identity,
                 multiplicative_identity, np_arg_sum=None):
        self.sum = np_sum
        self.multiply = np_multiply
        self.additive_identity = additive_identity
        self.multiplicative_identity = multiplicative_identity
        self.arg_sum = np_arg_sum

# Common semirings
Semiring.PLUS_MULTIPLY = Semiring(np.sum, np.multiply, 0, 1) # count/probability
Semiring.MAX_MULTIPLY = Semiring(np.max, np.multiply, -np.inf, 1, np.argmax)
Semiring.MAX_PLUS = Semiring(np.max, np.add, -np.inf, 0, np.argmax)


class ViterbiDecoder(StructuredDecoder):
    def __init__(self, possible_states=None, semiring=Semiring.MAX_MULTIPLY):
        self.possible_states = possible_states
        self.semiring = semiring

    # TODO: allow converting to log space to deal with numerical stability
    def run_viterbi(self, node_scores, transition_weights,
                    return_best_path=True):
        '''
        node_scores is a numpy array of scores for individual trellis nodes
            (size: num_states x num_sequence_items). Any start probabilities/
            weights are assumed to be folded into the first column of scores.
        transition_weights is one of:
          - a num_states x num_states array of scores for transitioning between
            states.
          - a (num_sequence_items-1) x num_states x num_states array of scores for
            transitioning between states for particular sequence items.
        if return_best_path is True, then instead of just returning the best
            score, the function will return (summed_score, best_state_path).
            (The semiring must have arg_sum defined for this.)
        '''
        # TODO: generalize code to higher Markov orders?
        assert self.semiring.arg_sum or not return_best_path, ('Can only return'
            ' best path for semirings with a defined arg_sum operation')

        # Declare arrays and initialize to base case values
        path_scores = np.empty(node_scores.shape)
        path_scores[:, 0] = node_scores[:, 0]
        if return_best_path:
            predecessors = np.empty(node_scores.shape, dtype=np.int32)
            predecessors[:, 0] = np.NaN

        transition_scores_by_item = transition_weights.ndim > 2
        # Recursive case: compute each trellis column based on previous column
        num_columns = node_scores.shape[1]
        for column_index in range(1, num_columns):
            # Find best predecessor state for each state.
            if transition_scores_by_item:
                pred_transition_weights = transition_weights[
                    column_index - 1, :, :] # -1 b/c #transitions = #items - 1
            else:
                pred_transition_weights = transition_weights
            # predecessor_scores will be num_states x num_states.
            # Rows represent start states and columns represent end states for
            # this transition.
            predecessor_scores = self.semiring.multiply(
                pred_transition_weights, path_scores[:, column_index - 1,
                                                     np.newaxis])

            if return_best_path:
                predecessor_indices = self.semiring.arg_sum(
                    predecessor_scores, axis=0) # "sum" over start states
                predecessors[:, column_index] = predecessor_indices
                # This "sum" is really a max or a min -- it just selects one
                # predecessor for each state.
                summed_scores = predecessor_scores[
                    predecessor_indices, range(len(predecessor_scores))]
            else:
                summed_scores = self.semiring.sum(predecessor_scores, axis=0)

            path_scores[:, column_index] = self.semiring.multiply(
                node_scores[:, column_index], summed_scores)

        if return_best_path:
            # Now reconstruct the best sequence from the predecessors matrix.
            best_state_path = np.empty((num_columns,), dtype=np.int32)
            best_final_index = self.semiring.arg_sum(path_scores[:, -1])
            best_state_path[-1] = best_final_index
            summed_score = path_scores[best_final_index, -1]
            for i in reversed(range(1, num_columns)):
                best_state_path[i - 1] = predecessors[best_state_path[i], i]

            if self.possible_states:
                best_state_path = [self.possible_states[i]
                                   for i in best_state_path]

            return summed_score, best_state_path
        else:
            summed_score = self.semiring.sum(path_scores[:, -1])
            return summed_score

    def decode(self, instance, instance_parts, scores):
        # (Rows = states, columns = sequence items.)
        best_score, best_path = self.run_viterbi(
            scores.node_scores, scores.transition_weights, True)

        logging.debug("Viterbi max score: %d", best_score)
        return best_path