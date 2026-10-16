"""ESCI 指标的手算例子与未标注结果边界。"""
import math

import pytest

from scripts.eval.metrics import graded_ndcg_at_k, judged_at_k


def test_graded_ndcg_matches_hand_calculated_four_level_gain():
    labels={'exact':'E','substitute':'S','complement':'C','irrelevant':'I'}
    ideal=1+.1/math.log2(3)+.01/math.log2(4)
    actual=.1+1/math.log2(3)+.01/math.log2(4)
    assert graded_ndcg_at_k(['substitute','exact','complement','irrelevant'],labels,4)==pytest.approx(actual/ideal)
    assert graded_ndcg_at_k(['exact','substitute','complement','irrelevant'],labels,4)==pytest.approx(1)


def test_unknown_results_have_zero_gain_and_annotation_order_is_irrelevant():
    labels={'exact':'E','substitute':'S'}
    expected=(1/math.log2(3))/(1+.1/math.log2(3))
    assert graded_ndcg_at_k(['unknown','exact'],labels,2)==pytest.approx(expected)
    assert graded_ndcg_at_k(['unknown','exact'],dict(reversed(list(labels.items()))),2)==pytest.approx(expected)


def test_cutoff_uses_the_best_known_grades_and_duplicates_do_not_shift_rank():
    labels={'a':'E','b':'E','c':'C'}
    expected=(1+.01/math.log2(3))/(1+1/math.log2(3))
    assert graded_ndcg_at_k(['a','a','c','b'],labels,2)==pytest.approx(expected)
    assert graded_ndcg_at_k(['a'],labels,1)==1


@pytest.mark.parametrize('labels',[{}, {'a':'I'}])
def test_empty_or_zero_gain_labels_score_zero(labels):
    assert graded_ndcg_at_k(['a'],labels,10)==0


def test_judged_counts_irrelevant_judgments_and_uses_returned_result_count():
    labels={'exact':'E','irrelevant':'I'}
    assert judged_at_k(['exact','irrelevant','unknown'],labels,10)==pytest.approx(2/3)
    assert judged_at_k(['irrelevant'],labels,10)==1
    assert judged_at_k(['exact','exact','unknown'],labels,2)==.5
    assert judged_at_k([],labels,10)==0


def test_nonpositive_cutoff_and_invalid_grades():
    assert graded_ndcg_at_k(['a'],{'a':'E'},0)==judged_at_k(['a'],{'a':'E'},0)==0
    with pytest.raises(ValueError,match='E、S、C、I'):
        graded_ndcg_at_k(['a'],{'a':'unknown'},10)
