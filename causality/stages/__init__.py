from data import CausationInstance

# Define a bunch of shared functions that are used by various stages in the
# pipeline.


def starts_before(token_1, token_2):
    # None, if present as an argument, should be second.
    return token_2 is None or (
        token_1 is not None and
        token_2.start_offset > token_1.start_offset)

def normalize_order(token_pair):
    '''
    Normalizes the order of a token pair so that the earlier one in the
    sentence is first in the pair.
    '''
    if starts_before(*token_pair):
        return tuple(token_pair)
    else:
        return (token_pair[1], token_pair[0])

def match_causation_pairs(expected_pairs, found_pairs, tp_pairs, fp_pairs,
                          fn_pairs):
    '''
    Match expected and predicted cause/effect pairs from a single sentence.
    expected_pairs and found_pairs are lists of Token tuples.
    *_instances are all lists in which to record the pairs of various sorts for
    later examination (ignored for any that are None).
    '''
    tp, fp, fn = 0, 0, 0
    found_pairs = [normalize_order(pair) for pair in found_pairs]
    expected_pairs = [normalize_order(pair) for pair in expected_pairs]

    for found_pair in found_pairs:
        try:
            expected_pairs.remove(found_pair)
            tp += 1
            if tp_pairs is not None:
                tp_pairs.append(found_pair)
        except ValueError: # found_pair wasn't in expected_pairs
            fp += 1
            if fp_pairs is not None:
                fp_pairs.append(found_pair)

    if fn_pairs is not None:
        fn_pairs.extend(expected_pairs)
    fn += len(expected_pairs)

    return tp, fp, fn

def print_instances_by_eval_result(tp_pairs, fp_pairs, fn_pairs):
    for pairs, pair_type in zip(
        [tp_pairs, fp_pairs, fn_pairs],
        ['True positives', 'False positives', 'False negatives']):
        print pair_type + ':'
        for pair in pairs:
            # If only one argument is present, it'll be in position 0.
            sentence = pair[0].parent_sentence
            print '    %s ("%s" / "%s")' % (
                sentence.original_text.replace('\n', ' '),
                pair[0].original_text,
                pair[1].original_text if pair[1] else None)

        print '\n'