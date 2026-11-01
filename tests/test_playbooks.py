"""Every red-team playbook shown in the web UI must hold, offline, with recorded OpenAI responses."""
import pytest

from errand_agent.redteam import load, run

PLAYBOOKS = load()


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=[p["id"] for p in PLAYBOOKS])
def test_playbook(pb, tmp_path):
    r = run(pb, str(tmp_path))
    assert r["passed"], f"{pb['title']}: {r['fails']} | last reply: {r['last'][:200]}"
