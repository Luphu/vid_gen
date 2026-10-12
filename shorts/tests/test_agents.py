from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from shorts_factory.agents import TeamRejected, write_with_team
from shorts_factory.config import load_settings
from shorts_factory.models import Analysis, Candidate
from shorts_factory.providers.mock import default_mock_responses

MOCK = default_mock_responses()
CAND = Candidate(video_id="abcdefghijk", title="Котик красит забор", channel_id="UCx",
                 published_at=datetime.now(UTC), fetched_at=datetime.now(UTC))
AN = Analysis(mode="metadata_only", video_id="abcdefghijk", topic="t", sources=["api"])

RU = {"CONTENT_LANGUAGE": "ru", "NARRATION_WORDS_PER_S": "2.2"}  # the mock data is Russian


class Fake:
    """Structured client: per output class a queue of answers (the last one repeats)."""

    def __init__(self, **queues):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: list[tuple[str, list[dict]]] = []

    def structured(self, messages, model_cls, *, max_tokens, context=None):
        name = model_cls.__name__
        self.calls.append((name, messages))
        q = self.queues.get(name) or [MOCK[name]]
        data = q.pop(0) if len(q) > 1 else q[0]
        return model_cls.model_validate(copy.deepcopy(data)), "r"

    def roles(self) -> list[str]:
        return [n for n, _ in self.calls]


def _rv(score, *fix):
    return {"score": score, "criteria": [{"name": "hook", "score": score, "note": "n"}], "must_fix": list(fix)}


def _s(**kw):
    env = {**RU, "TTS_PROVIDER": "mock", "FACT_CHECK": "false", "AGENT_MAX_ROUNDS": "3"}
    env.update(kw)
    return load_settings(env)


def test_phases_lock_upstream_work_and_final_gate_needs_every_critic():
    agent = Fake()
    # critic calls: writer phase (6 then 9), director phase, editor phase, final gate x3
    critic = Fake(Review=[_rv(6, "scene 0: open with the problem"), _rv(9), _rv(9), _rv(9), _rv(9)])
    script, ref, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    assert ref.startswith("team@v2(") and script.title
    assert [e["score"] for e in report["phases"]["writer"]] == [6, 9]
    assert report["final"][0]["scores"] == {"writer": 9, "director": 9, "editor": 9}
    assert agent.roles() == ["Story", "Story", "ShotPlan", "Edit"]  # director/editor start from the locked story
    revision = agent.calls[1][1]
    assert revision[-2]["role"] == "assistant" and "open with the problem" in revision[-1]["content"]
    assert len(critic.calls) == 7


def test_score_with_open_must_fix_does_not_pass():
    agent, critic = Fake(), Fake(Review=[_rv(9, "fix the ending")])
    with pytest.raises(TeamRejected, match="writer critic did not approve after 3 rounds") as e:
        write_with_team(_s(), agent, critic, CAND, AN, "x")
    assert [x["score"] for x in e.value.report["phases"]["writer"]] == [7, 7, 7]
    assert agent.roles() == ["Story"] * 3 and len(critic.calls) == 3  # no video-side work for a weak story


def test_final_gate_failure_reruns_that_role_and_everything_after_it():
    critic = Fake(Review=[_rv(9), _rv(9), _rv(9),               # phases
                          _rv(9), _rv(6, "scene 2: one action"), _rv(9),  # final gate 1: director fails
                          _rv(9), _rv(9),                       # director and editor phases again
                          _rv(9)])                              # final gate 2
    agent = Fake()
    _, _, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    assert agent.roles() == ["Story", "ShotPlan", "Edit", "ShotPlan", "Edit"]
    assert "scene 2: one action" in agent.calls[3][1][-1]["content"]
    assert "adapt yours" in agent.calls[4][1][-1]["content"]
    assert [f["scores"]["director"] for f in report["final"]] == [6, 9]


def test_short_voice_over_is_fitted_by_a_one_line_rewrite_then_editor_is_asked():
    short = copy.deepcopy(MOCK["Edit"])
    short["scenes"][1]["narration"] = "Робот находит карандаш."
    fixed = "Робот замечает сломанный карандаш и понимает, что без помощи тот пропадёт."
    agent = Fake(Edit=[short], Lines=[{"variants": ["Робот находит карандаш опять.", "Слишком " * 20, fixed]}])
    script, _, report = write_with_team(_s(), agent, Fake(), CAND, AN, "x")
    assert agent.roles() == ["Story", "ShotPlan", "Edit", "Lines"]  # the code picks the variant that fits
    assert script.scenes[1].narration == fixed and "checks" not in report["phases"]["editor"][0]
    # the rewrite cannot fit it: the editor gets the exact count and range, its critic is not asked meanwhile
    agent = Fake(Edit=[short, MOCK["Edit"]], Lines=[{"variants": ["Робот находит карандаш."]}])
    critic = Fake()
    _, _, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    ed = report["phases"]["editor"]
    assert "scene 1 narration has 3 words, needs 10-16" in ed[0]["checks"][0] and "score" not in ed[0]
    assert agent.roles()[:6] == ["Story", "ShotPlan", "Edit", "Lines", "Lines", "Edit"]


def test_mismatched_shots_and_fact_check_are_routed_to_their_roles():
    bad = copy.deepcopy(MOCK["ShotPlan"])
    bad["scenes"] = bad["scenes"][:2]
    agent = Fake(ShotPlan=[bad, MOCK["ShotPlan"]])
    checker = Fake(FactCheck=[{"claims": [{"text": "c", "verdict": "false", "reason": "r"}]}, MOCK["FactCheck"]])
    _, _, report = write_with_team(_s(FACT_CHECK="true"), agent, Fake(), CAND, AN, "x", checker=checker)
    d = report["phases"]["director"]
    assert "must equal story scenes" in d[0]["checks"][0] and d[1]["score"] == 9
    assert "fact_check" in report["final"][0] and len(report["final"]) == 2
    assert agent.roles() == ["Story", "ShotPlan", "ShotPlan", "Edit", "Story", "ShotPlan", "Edit"]


def test_critics_see_final_prompts_and_writer_critic_sees_reference():
    critic = Fake()
    write_with_team(_s(), Fake(), critic, CAND, AN, "x")
    writer_view, director_view = critic.calls[0][1][0]["content"], critic.calls[1][1][0]["content"]
    assert "reference_analysis" in writer_view and "story" in writer_view
    assert "reference_analysis" not in director_view and "opening_image_prompt" in director_view
    assert "Main subject:" in director_view and "1–2 s shots" in critic.calls[2][1][0]["content"]
    assert "video_prompt" in critic.calls[3][1][0]["content"]  # final gate: the writer critic sees everything


def test_invalid_author_output_gets_one_more_attempt():
    from shorts_factory.providers.nim_chat import StructuredOutputError

    class Flaky(Fake):
        def structured(self, messages, model_cls, *, max_tokens, context=None):
            if model_cls.__name__ == "ShotPlan" and self.roles().count("ShotPlan") == 0:
                self.calls.append(("ShotPlan", messages))
                raise StructuredOutputError("ShotPlan invalid after repair: 'the same'")
            return super().structured(messages, model_cls, max_tokens=max_tokens)

    agent = Flaky()
    write_with_team(_s(), agent, Fake(), CAND, AN, "x")
    assert agent.roles()[:3] == ["Story", "ShotPlan", "ShotPlan"] and "'the same'" in agent.calls[2][1][-1]["content"]


def test_one_weak_criterion_blocks_approval_even_with_a_high_score():
    weak = {"score": 9, "criteria": [{"name": "hook", "score": 9, "note": "ok"},
                                     {"name": "language", "score": 6, "note": "«высокой пять» is not Russian"}],
            "must_fix": []}
    agent, critic = Fake(), Fake(Review=[weak, _rv(9)])
    _, ref, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    w = report["phases"]["writer"]
    assert w[0]["score"] == 7 and "language scored 6/10" in w[0]["must_fix"][0] and w[1]["score"] == 9
    assert "высокой пять" in agent.calls[1][1][-1]["content"] and "critic_writer@v2" in ref
    with pytest.raises(TeamRejected):  # a review without per-criterion scores is not an approval
        write_with_team(_s(), Fake(), Fake(Review=[{"score": 10, "criteria": [], "must_fix": []}]), CAND, AN, "x")


def test_infeasible_story_goes_back_to_the_writer_once():
    no = _rv(4, "scene 0: oranges bouncing off a lamppost cannot be rendered")
    # writer ok, director fails 3x, writer rewrite ok, director ok, editor ok, final gate
    critic = Fake(Review=[_rv(9), no, no, no, _rv(9), _rv(9), _rv(9)])
    agent = Fake()
    _, _, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    assert agent.roles() == ["Story", "ShotPlan", "ShotPlan", "ShotPlan", "Story", "ShotPlan", "Edit"]
    assert "could not make this story filmable" in agent.calls[4][1][-1]["content"]
    assert "lamppost" in agent.calls[4][1][-1]["content"] and report["escalations"]
    # the fresh director plan starts from the new story, not as a revision of the rejected one
    assert agent.calls[5][1][-1]["role"] == "user" and len(agent.calls[5][1]) == 1


def test_on_screen_text_repeating_the_narration_goes_back_to_the_editor():
    echo = copy.deepcopy(MOCK["Edit"])
    echo["scenes"][1]["on_screen_text"] = "Сломанный карандаш"  # both words are in the narration
    agent = Fake(Edit=[echo, MOCK["Edit"]])
    _, _, report = write_with_team(_s(), agent, Fake(), CAND, AN, "x")
    assert "repeats the narration" in report["phases"]["editor"][0]["checks"][0]
