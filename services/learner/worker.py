import os
import httpx
from celery import Celery
redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
app = Celery("learner", broker = redis_url, backend = redis_url)
app.conf.task_routes = {
      "trigger_learning": {"queue": "learning"},
}

# Consumes the "trigger_learning" messages services/webhook publishes to the
# "learning" queue. The name and args must match the webhook's sender.
@app.task(name = "trigger_learning")
def trigger_learning(repo_full_name : str, pr_id : str):
    with httpx.Client() as client:
        response = client.post(
            "http://learner:8004/learn",
            json = {"repo_full_name": repo_full_name, "pr_id": pr_id},
        )
        response.raise_for_status()
