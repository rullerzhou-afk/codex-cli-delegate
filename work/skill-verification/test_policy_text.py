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
AGENT_METADATA = REPO / "skill" / "agents" / "openai.yaml"
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
    "established_alias": ["established user alias", "equivalent to naming that canonical external route"],
    "canonical_deduplication": ["merge repeated labels that resolve to the same backend",
                                "at most one job per distinct canonical backend"],
    "matching_backend": ["Before reporting that the route was started or satisfied",
                         "read the saved backend from the start record or `delegate_status`"],
    "native_identity": ["must not label or report any worker as an external backend unless that saved backend matches",
                        "a native worker has no matching external job and cannot satisfy the route"],
    "native_verification": ["Report model and effort as verified only after native completion verification"],
    "multiple_named_routes": ["explicitly names multiple distinct canonical external routes",
                              "one matching job per backend"],
    "checkout_reservation": ["Every job that has not released its reservation occupies its checkout",
                             "including read-only jobs", "same checkout and nested paths conflict",
                             "Concurrent jobs require distinct, non-overlapping worktrees or clones",
                             "Do not use a different state root to bypass this reservation",
                             "prevents conflict detection across state roots"],
    "read_only_reviewers": ["Several read-only reviewers may inspect the same subject",
                            "reservations apply regardless of tool profile"],
    "no_native_substitution": ["including by using a native worker"],
    "scenarios_non_proof": ["human-observation aids", "not proof of stable model routing or quota savings"],
}

SKILL_REQUIRED = [
    "new external job requires both an explicit request for Claude, Kimi, or OpenCode",
    "one whole coherent responsibility",
    "coordination cost",
    "Neither condition alone is enough",
    "allowed resolution paths",
    "recovering it after interruption or context loss",
    "Resolve every canonical name and established alias to its backend before dispatch",
    "merge repeated labels that resolve to the same backend",
    "at most one job per distinct canonical backend",
    "confirming its saved `backend` in the start record or `delegate_status`",
    "Report model and effort only after native completion verification",
    "must not label or report any worker as an external backend unless that saved backend matches",
    "a native worker has no matching external job and cannot satisfy the route",
    "multiple distinct external routes",
    "one matching job per backend",
    "several read-only reviewers may inspect the same subject",
    "Every job that has not released its reservation occupies its checkout regardless of tool profile",
    "concurrent jobs, including read-only reviews, require distinct non-overlapping worktrees or clones",
    "the same checkout and its nested paths conflict",
    "do not bypass this reservation with another state root",
    "disables conflict detection across state roots",
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
    "including by using a native worker",
    "references/delegation-policy.md",
]

README_REQUIRED = [
    "explicit external route",
    "new external job requires both an explicit request for Claude, Kimi, or OpenCode",
    "one whole coherent responsibility",
    "coordination cost",
    "allowed resolution paths",
    "Resolve canonical names and established aliases to backends before dispatch",
    "merge labels that resolve to the same backend",
    "at most one job per distinct canonical backend",
    "saved `backend` is confirmed from the start record or status",
    "model and effort claims also require native completion verification",
    "must not label or report any worker as an external backend unless that saved backend matches",
    "a native worker cannot satisfy the route",
    "multiple distinct external routes",
    "one matching job is dispatched per backend",
    "Several read-only reviewers may inspect the same subject",
    "every job holding a reservation occupies its checkout regardless of tool profile",
    "Concurrent jobs, including read-only reviews, require distinct non-overlapping worktrees or clones",
    "the same checkout and nested paths conflict",
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
    "including by using a native worker",
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
    "正式名称和已建立别名解析成 canonical backend",
    "解析到同一 backend 的重复名称合并",
    "每个不同 backend 最多启动一个 job",
    "确认保存的 `backend` 匹配后，才可以汇报点名路线已经启动或满足",
    "模型和 effort 还要等待原生完成证据核验",
    "不得把任何 worker 标记或汇报成与其保存 backend 不符的外部路线",
    "原生 worker 不能满足该路线",
    "多个不同的外部路线",
    "每个 backend 分别派发一个匹配 job",
    "多个只读审查者可以检查同一对象",
    "每个尚未释放预订的 job 都会占用 checkout，不区分只读或写入",
    "并发 job 即使都是只读审查，也必须使用互不重叠的 worktree 或 clone",
    "同一 checkout 及其父子路径会冲突",
    "明确要求的外部路由不可用",
    "绝不静默改用其他路由、模型或账号",
    "不能改用原生 worker 顶替",
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

AGENT_REQUIRED = [
    "start MCP jobs only when an explicit external route and one coherent responsibility are both present",
    "no negative trigger applies",
    "Resolve and deduplicate aliases to canonical backends",
    "one job per distinct backend",
    "a non-overlapping checkout for each concurrent job",
    "verify the saved backend before reporting identity",
]

FORBIDDEN_EN = [
    "native worker may be reported as",
    "may substitute a native worker",
    "read-only reviewers may share one checkout",
    "one job per route name",
]

FORBIDDEN_ZH = [
    "可以改用原生 worker 顶替",
    "只读审查可以共用同一个 checkout",
    "每个点名名称一个 job",
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

    def assert_forbidden_absent(self, text, phrases, label):
        normalized = normalise(text)
        present = [phrase for phrase in phrases if normalise(phrase) in normalized]
        self.assertEqual(present, [], "%s contains contradictory policy phrases: %s" % (label, present))

    def test_reference_covers_every_concept(self):
        text = read(REFERENCE)
        for concept, phrases in REFERENCE_REQUIRED.items():
            with self.subTest(concept=concept):
                self.assert_phrases(text, phrases, "delegation-policy.md (%s)" % concept)
        self.assert_forbidden_absent(text, FORBIDDEN_EN, "delegation-policy.md")

    def test_skill_states_triggers_and_precedence(self):
        self.assert_phrases(read(SKILL), SKILL_REQUIRED, "SKILL.md")
        self.assert_forbidden_absent(read(SKILL), FORBIDDEN_EN, "SKILL.md")

    def test_skill_frontmatter_states_explicit_request(self):
        frontmatter = read(SKILL).split("---", 2)[1].lower()
        self.assertIn("explicitly requested", frontmatter)
        self.assertIn("continue or recover", frontmatter)
        self.assertIn("claude", frontmatter)
        self.assertIn("kimi", frontmatter)
        self.assertIn("opencode", frontmatter)
        self.assert_phrases(read(AGENT_METADATA), AGENT_REQUIRED, "agents/openai.yaml")
        self.assert_forbidden_absent(read(AGENT_METADATA),
                                     FORBIDDEN_EN + ["for every explicitly named external backend"],
                                     "agents/openai.yaml")

    def test_readme_states_user_visible_policy(self):
        self.assert_phrases(read(README), README_REQUIRED, "README.md")
        self.assert_forbidden_absent(read(README), FORBIDDEN_EN, "README.md")

    def test_chinese_readme_is_synchronized(self):
        self.assert_phrases(read(README_ZH), README_ZH_REQUIRED, "README.zh-CN.md")
        self.assert_forbidden_absent(read(README_ZH), FORBIDDEN_ZH, "README.zh-CN.md")

    def test_reference_is_linked_from_every_public_doc(self):
        for path in (SKILL, README, README_ZH):
            with self.subTest(path=path.name):
                self.assertIn("delegation-policy.md", read(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
