"""Durable text checks for the Phase 1 external-delegation policy.

These verify that the trigger and co-installation precedence text stays present
across the canonical reference, the Skill, and both READMEs. They are not a
routing test: passing them does not prove a model will always choose the
desired route, and they cannot measure quota savings.
"""
from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / "skill" / "references" / "delegation-policy.md"
SKILL = REPO / "skill" / "SKILL.md"
README = REPO / "README.md"
README_ZH = REPO / "README.zh-CN.md"

# Concept -> phrases that must all appear. Kept as stable semantic anchors, not
# a verbatim copy of the prose, so wording can improve without weakening intent.
REFERENCE_REQUIRED = {
    "combined_gate": ["new external job requires both", "explicit request for Claude, Kimi, or OpenCode",
                      "whole coherent responsibility", "coordination cost"],
    "gate_neither_alone": ["Neither condition alone opens the gate"],
    "resolution_paths": ["Continuation and recovery of an external job this Skill already started",
                         "allowed resolution paths"],
    "negative_native": ["native worker only"],
    "negative_solo": ["solo work"],
    "negative_casual": ["casual explanation"],
    "negative_tiny": ["work is tiny"],
    "negative_nearly_complete": ["already nearly complete"],
    "one_worker": ["one external worker for tightly coupled investigation"],
    "forward_constraints": ["Forward new constraints to an active worker promptly"],
    "same_worker_rework": ["Continue the same worker and job for rework", "phase-named"],
    "independent_acceptance": ["Independent acceptance by Codex remains mandatory"],
    "no_second_reviewer": ["second external reviewer by default"],
    "adversarial_review": ["adversarial review only when the user requests it"],
    "controls_not_sandbox": ["tool allowlists", "not an operating-system sandbox"],
    "precedence": ["does not claim routing precedence"],
    "recover_previously_started": ["external jobs its tools previously started"],
    "host_native_outside": ["neither observes nor controls that worker"],
    "reservation_release": ["through release of its reservation"],
    "unavailable_route": ["external route is unavailable, report it",
                          "do not silently substitute a route, model, or account"],
    "scenarios_non_proof": ["human-observation aids", "not proof of stable model routing or quota savings"],
}

SKILL_REQUIRED = [
    "new external job requires both an explicit request for Claude, Kimi, or OpenCode",
    "one whole coherent responsibility",
    "coordination cost",
    "Neither condition alone is enough",
    "allowed resolution paths",
    "recovering it after interruption or context loss",
    "native-worker-only requests",
    "solo work",
    "casual explanations",
    "tiny work",
    "already nearly complete",
    "continue the same worker and job for rework",
    "phase-named jobs",
    "Independent Codex acceptance stays mandatory",
    "second external reviewer by default",
    "adversarial review",
    "not an operating-system sandbox",
    "routing precedence",
    "previously started",
    "neither observes nor controls",
    "through release of its reservation",
    "explicitly requested external route is unavailable",
    "never silently substitute a route, model, or account",
    "references/delegation-policy.md",
]

README_REQUIRED = [
    "explicit external route",
    "new external job requires both an explicit request for Claude, Kimi, or OpenCode",
    "one whole coherent responsibility",
    "coordination cost",
    "allowed resolution paths",
    "native-worker-only requests",
    "solo work",
    "casual explanations",
    "tiny work",
    "already nearly complete",
    "continue the same worker and job for rework",
    "phase-named jobs",
    "second external reviewer by default",
    "adversarial review",
    "not an operating-system sandbox",
    "routing precedence",
    "previously started",
    "through release of its reservation",
    "explicitly requested external route is unavailable",
    "never silently substitute a route, model, or account",
    "skill/references/delegation-policy.md",
]

README_ZH_REQUIRED = [
    "新开外部任务需要同时满足",
    "明确要求 Claude",
    "续接本 skill 已启动的外部任务",
    "恢复它",
    "完整、连贯",
    "协调成本",
    "允许的处置路径",
    "明确要求的外部路由不可用",
    "绝不静默改用其他路由、模型或账号",
    "只用原生 worker",
    "独立完成",
    "闲聊式解释",
    "任务极小",
    "接近完成",
    "同一 worker、同一 job",
    "阶段命名",
    "独立验收始终必需",
    "第二个外部审查者",
    "对抗性审查",
    "不是操作系统沙箱",
    "不宣称路由优先级",
    "先前启动的外部任务",
    "既不观察也不控制",
    "释放预订为止",
    "skill/references/delegation-policy.md",
]


def read(path):
    return path.read_text(encoding="utf-8")


def normalise(text):
    # Collapse line wrapping so a phrase can span source line breaks.
    return " ".join(text.split()).lower()


class PolicyText(unittest.TestCase):
    def assert_phrases(self, text, phrases, label):
        normalized = normalise(text)
        missing = [phrase for phrase in phrases if normalise(phrase) not in normalized]
        self.assertEqual(missing, [], "%s is missing policy phrases: %s" % (label, missing))

    def test_reference_covers_every_concept(self):
        text = read(REFERENCE)
        for concept, phrases in REFERENCE_REQUIRED.items():
            with self.subTest(concept=concept):
                self.assert_phrases(text, phrases, "delegation-policy.md (%s)" % concept)

    def test_skill_states_triggers_and_precedence(self):
        self.assert_phrases(read(SKILL), SKILL_REQUIRED, "SKILL.md")

    def test_skill_frontmatter_states_explicit_request(self):
        frontmatter = read(SKILL).split("---", 2)[1].lower()
        self.assertIn("explicitly requested", frontmatter)
        self.assertIn("continue or recover", frontmatter)
        self.assertIn("claude", frontmatter)
        self.assertIn("kimi", frontmatter)
        self.assertIn("opencode", frontmatter)

    def test_readme_states_user_visible_policy(self):
        self.assert_phrases(read(README), README_REQUIRED, "README.md")

    def test_chinese_readme_is_synchronized(self):
        self.assert_phrases(read(README_ZH), README_ZH_REQUIRED, "README.zh-CN.md")

    def test_reference_is_linked_from_every_public_doc(self):
        for path in (SKILL, README, README_ZH):
            with self.subTest(path=path.name):
                self.assertIn("delegation-policy.md", read(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
