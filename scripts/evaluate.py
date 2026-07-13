import os
import sys

import psycopg2
from datasets import Dataset
from langchain_anthropic import ChatAnthropic
from ragas import evaluate
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import faithfulness

def main():
    database_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(database_url)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                           SELECT f.message, f.diff
                           FROM findings f
                           JOIN pull_requests pr ON pr.id = f.pr_id
                           WHERE f.diff IS NOT NULL
                           ORDER BY f.created_at DESC
                           LIMIT 50
                           """
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
    finally:
        conn.close()

    if not rows:
        print("No findings with a recorded diff found, skipping evaluation.")
        sys.exit(0)

    data = {
        "question" : [],
        "answer": [],
        "contexts": []

    }

    for message, diff in rows:
        data["question"].append("What issues exist in this code?")
        data["answer"].append(message or "")
        data["contexts"].append([diff or ""])

    dataset = Dataset.from_dict(data)

    judge_llm = LangchainLLMWrapper(ChatAnthropic(model = "claude-sonnet-4-6"))
    results = evaluate(dataset, metrics = [faithfulness], llm = judge_llm)

    print("Evaluation results:")
    print(results)

    scores = results.to_pandas()
    if "faithfulness" not in scores.columns:
        print("Faithfulness metric did not produce a score; failing the evaluation.")
        sys.exit(1)

    faithfulness_score = scores["faithfulness"].mean()
    print(f"Mean faithfulness: {faithfulness_score:.4f}")

    if faithfulness_score < 0.7:
        print(f"Faithfulness score {faithfulness_score:.4f} is below threshold 0.7")
        sys.exit(1)

if __name__ == "__main__":
    main()
