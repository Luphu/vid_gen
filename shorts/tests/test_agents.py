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

RU = {"CONTENT_LANGUAGE": "ru", "NARRATION_WORDS_PER_S": "2.2",  # the mock data is Russian
      "TTS_VOICES": '{"Mock_Voice": {"desc": "test voice", "wps": 2.2}}'}


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
    # final gate: the writer was approved on the story alone and the director before the editor chose the
    # voice -> both asked again; the editor approved exactly this package -> not asked again
    assert len(critic.calls) == 6


def test_score_with_open_must_fix_does_not_pass():
    agent, critic = Fake(), Fake(Review=[_rv(9, "fix the ending")])
    with pytest.raises(TeamRejected, match="writer critic did not approve after 3 rounds") as e:
        write_with_team(_s(), agent, critic, CAND, AN, "x")
    assert [x["score"] for x in e.value.report["phases"]["writer"]] == [7, 7, 7]
    assert agent.roles() == ["Story"] * 3 and len(critic.calls) == 3  # no video-side work for a weak story


def test_final_gate_failure_reruns_that_role_and_everything_after_it():
    edited = copy.deepcopy(MOCK["Edit"])
    edited["scenes"][2]["on_screen_text"] = "Как новенький"  # the editor changes the text after the director
    critic = Fake(Review=[_rv(9), _rv(9), _rv(9),                  # phases
                          _rv(9), _rv(6, "scene 2: one action"),   # final gate 1: writer ok, director fails;
                          _rv(9), _rv(9),                          # the editor approved this exact package
                          _rv(9)])                                 # director, editor phases; final gate 2: writer
    agent = Fake(Edit=[edited])
    _, _, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    assert agent.roles() == ["Story", "ShotPlan", "Edit", "ShotPlan", "Edit"]
    assert "scene 2: one action" in agent.calls[3][1][-1]["content"]
    assert "adapt yours" in agent.calls[4][1][-1]["content"]
    assert [f["scores"]["director"] for f in report["final"]] == [6, 9] and len(critic.calls) == 8


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
                                     {"name": "language", "score": 5, "note": "«высокой пять» is not Russian"}],
            "must_fix": []}
    agent, critic = Fake(), Fake(Review=[weak, _rv(9)])
    _, ref, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    w = report["phases"]["writer"]
    assert w[0]["score"] == 7 and "language scored 5/10" in w[0]["must_fix"][0] and w[1]["score"] == 9
    assert "высокой пять" in agent.calls[1][1][-1]["content"] and "critic_writer@v4" in ref
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


def test_viewer_who_cannot_follow_sends_the_story_back_to_the_writer():
    lost = {"summary": "A man and a sandwich... a crate?", "payoff": "none", "confusing": ["the crate"],
            "understood": False}
    viewer = Fake(ViewerReport=[lost, MOCK["ViewerReport"]], ViewerMatch=[{"match": False, "gap": "ending"},
                                                                         MOCK["ViewerMatch"]])
    # gate 1: first viewer lost, second understood -> still fails (both must follow); gate 2: both understood
    agent, critic = Fake(), Fake()
    _, _, report = write_with_team(_s(), agent, critic, CAND, AN, "x", viewer=viewer)
    f = report["final"]
    assert not f[0]["viewer"]["ok"] and f[0]["scores"] == {} and f[1]["viewer"]["ok"]
    assert agent.roles() == ["Story", "ShotPlan", "Edit", "Story", "ShotPlan", "Edit"]
    assert "viewer test" in agent.calls[3][1][-1]["content"] and "crate" in agent.calls[3][1][-1]["content"]
    blind = viewer.calls[0][1][0]["content"]
    assert "concept" not in blind and "voice_over" in blind  # the viewer never sees the writer's intent


def test_director_must_put_key_props_into_the_setting_and_shots():
    from shorts_factory.agents import ShotPlan, director_checks

    plan = copy.deepcopy(MOCK["ShotPlan"])
    plan["key_props"] = ["a tiny brass bell"]
    assert any("not described in the setting" in c for c in director_checks(ShotPlan.model_validate(plan)))
    plan["setting"] += ", a tiny brass bell on the desk"
    assert any("not in must_show of any shot" in c for c in director_checks(ShotPlan.model_validate(plan)))
    plan["scenes"][0]["must_show"] = ["a tiny brass bell"]
    assert director_checks(ShotPlan.model_validate(plan)) == []
    bad = copy.deepcopy(plan)
    bad["scenes"][1]["must_show"] = ["a dragon"]
    assert any("not one of key_props" in c for c in director_checks(ShotPlan.model_validate(bad)))


def test_editor_chooses_a_listed_voice_and_its_speech_rate_sets_the_word_count():
    from shorts_factory.config import load_settings
    from shorts_factory.scripting import narration_range

    real = load_settings({})
    fast, slow = (narration_range(real, 6, v)[0] for v in ("English_CaptivatingStoryteller", "English_Wiselady"))
    assert fast > slow
    assert "young female" in real.tts_voices["English_PlayfulGirl"]["desc"]
    s = _s()
    script, _, _ = write_with_team(s, Fake(), Fake(), CAND, AN, "x")
    assert script.voice == "Mock_Voice"
    odd = copy.deepcopy(MOCK["Edit"])
    odd["voice"] = "Robot_9000"
    agent = Fake(Edit=[odd, MOCK["Edit"]])
    _, _, report = write_with_team(s, agent, Fake(), CAND, AN, "x")
    assert any("Robot_9000" in c for c in report["phases"]["editor"][0]["checks"])
    critic = Fake()
    write_with_team(s, Fake(), critic, CAND, AN, "x")
    assert "Mock_Voice: test voice" in critic.calls[2][1][0]["content"]  # the editor critic sees the voice list


def test_edu_mode_asks_for_one_true_fact_and_the_story_critic_checks_it():
    agent, critic = Fake(), Fake()
    s = _s(CONTENT_MODE="edu")
    write_with_team(s, agent, critic, CAND, AN, "a common myth busted by the hero")
    writer_prompt, story_critic = agent.calls[0][1][0]["content"], critic.calls[0][1][0]["content"]
    assert "EDUCATIONAL short" in writer_prompt and "ONE surprising, TRUE fact" in writer_prompt
    assert "a common myth busted by the hero" in writer_prompt and "$" not in writer_prompt.split("<untrusted")[0]
    assert "- fact:" in story_critic
    story = Fake()
    write_with_team(_s(), story, Fake(), CAND, AN, "x")
    assert "CORE SITUATION" in story.calls[0][1][0]["content"] and "EDUCATIONAL" not in story.calls[0][1][0]["content"]


def test_edu_doubtful_fact_goes_back_to_the_writer_before_the_director_works():
    doubtful = {"claims": [{"text": "a chop collapses the air pockets", "verdict": "uncertain", "reason": "r"}]}
    checker = Fake(FactCheck=[doubtful, MOCK["FactCheck"]])
    agent = Fake()
    _, _, report = write_with_team(_s(CONTENT_MODE="edu", FACT_CHECK="true"), agent, Fake(), CAND, AN, "x",
                                   checker=checker)
    w = report["phases"]["writer"]
    assert "air pockets" in w[0]["checks"][0] and "score" not in w[0] and w[1]["score"] == 9
    assert agent.roles()[:3] == ["Story", "Story", "ShotPlan"]


def test_a_six_without_blocking_defects_is_polish_not_a_rejection():
    polish = {"score": 8, "criteria": [{"name": "packaging", "score": 6, "note": "slightly technical"},
                                       {"name": "hook", "score": 9, "note": "ok"}], "must_fix": []}
    _, _, report = write_with_team(_s(), Fake(), Fake(Review=[polish]), CAND, AN, "x")
    assert report["phases"]["writer"][0]["score"] == 8


def test_editor_that_cannot_land_the_ending_escalates_to_the_writer_once():
    no = _rv(6, "the last line does not land the payoff")
    # writer ok, director ok, editor fails 3x -> writer rewrite ok, director ok, editor ok, final gate
    critic = Fake(Review=[_rv(9), _rv(9), no, no, no, _rv(9), _rv(9), _rv(9)])
    agent = Fake()
    _, _, report = write_with_team(_s(), agent, critic, CAND, AN, "x")
    assert [e["from"] for e in report["escalations"]] == ["editor"]
    assert agent.roles() == ["Story", "ShotPlan", "Edit", "Edit", "Edit", "Story", "ShotPlan", "Edit"]
    assert "payoff" in agent.calls[5][1][-1]["content"] and len(agent.calls[6][1]) == 1  # fresh director


def test_director_starts_fresh_when_the_story_changes_its_scene_count():
    from shorts_factory.agents import ShotPlan, Story, _author, _values

    shorter = copy.deepcopy(MOCK["Story"])
    shorter["scenes"] = shorter["scenes"][:2]
    s = _s()
    vals = _values(s, {"topic": "t"}, "x")
    out = {"writer": Story.model_validate(shorter), "director": ShotPlan.model_validate(MOCK["ShotPlan"])}
    agent = Fake()
    _author(s, agent, "director", vals, out, ["The writer's work changed; adapt yours to it."])
    assert len(agent.calls[0][1]) == 1  # a fresh plan for 2 scenes, not a revision of the old 3-shot plan
    same = {"writer": Story.model_validate(MOCK["Story"]), "director": ShotPlan.model_validate(MOCK["ShotPlan"])}
    _author(s, agent, "director", vals, same, ["fix scene 1"])
    assert agent.calls[1][1][-2]["role"] == "assistant"  # same scenes: a normal revision
