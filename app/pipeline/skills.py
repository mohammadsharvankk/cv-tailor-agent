"""
Skill vocabulary with aliases.

Two different matching modes are used on purpose:

* `literal_hit`  - case-insensitive whole-phrase match of the employer's exact wording.
                   This is how most applicant tracking systems (ATS) work, so it drives the ATS score.
* `supported`    - alias-aware match ("sklearn" supports "scikit-learn"). This drives the fact guard:
                   the tool may switch your wording to the employer's, but only for skills your CV
                   already shows under some name.
"""

from __future__ import annotations

import re
from functools import lru_cache

# canonical name -> aliases (lower-case). Canonical name is matched too.
SKILL_ALIASES: dict[str, list[str]] = {
    # languages
    "Python": ["python3", "py"],
    "SQL": ["t-sql", "pl/sql", "mysql", "postgresql", "postgres", "sql server", "sqlite"],
    "R": ["r programming", "rstudio", "tidyverse"],
    "Java": [],
    "JavaScript": ["js", "node.js", "nodejs"],
    "TypeScript": ["ts"],
    "Scala": [],
    "Go": ["golang"],
    "C++": ["cpp"],
    "C#": ["csharp", ".net"],
    "Bash": ["shell scripting", "shell"],
    # data / ML libraries
    "pandas": [],
    "NumPy": ["numpy"],
    "scikit-learn": ["sklearn", "scikit learn"],
    "XGBoost": ["xgb"],
    "LightGBM": [],
    "PyTorch": ["torch"],
    "TensorFlow": ["keras", "tf"],
    "Hugging Face": ["huggingface", "transformers"],
    "SHAP": [],
    "spaCy": [],
    "NLTK": [],
    "OpenCV": [],
    "Matplotlib": [],
    "Seaborn": [],
    "Plotly": [],
    "Statsmodels": [],
    # ML concepts
    "Machine Learning": ["ml"],
    "Deep Learning": ["neural networks", "neural network"],
    "NLP": ["natural language processing"],
    "Computer Vision": [],
    "Time Series": ["time-series", "forecasting"],
    "Anomaly Detection": [],
    "Classification": ["classifier", "classifiers"],
    "Regression": [],
    "Feature Engineering": [],
    "A/B Testing": ["ab testing", "a/b tests", "experimentation"],
    "Statistics": ["statistical analysis", "statistical modelling", "statistical modeling"],
    "Explainable AI": ["xai", "explainability", "model interpretability"],
    "Cross-Validation": ["cross validation"],
    # GenAI
    "LLM": ["llms", "large language models", "large language model"],
    "RAG": ["retrieval-augmented generation", "retrieval augmented generation"],
    "Prompt Engineering": [],
    "LangChain": [],
    "LlamaIndex": [],
    "OpenAI API": ["openai", "gpt-4", "gpt"],
    "Anthropic API": ["anthropic", "claude"],
    "Vector Databases": ["vector database", "vector db", "pinecone", "faiss", "chroma", "pgvector", "weaviate"],
    "Embeddings": ["embedding"],
    "AI Agents": ["agents", "agentic", "ai agent"],
    # data engineering
    "ETL": ["elt", "data pipelines", "data pipeline"],
    "Apache Spark": ["spark", "pyspark"],
    "Airflow": ["apache airflow"],
    "dbt": [],
    "Kafka": ["apache kafka"],
    "Snowflake": [],
    "BigQuery": [],
    "Redshift": [],
    "Databricks": [],
    "Data Warehousing": ["data warehouse"],
    "Data Modelling": ["data modeling", "dimensional modelling", "dimensional modeling"],
    "NoSQL": ["mongodb", "cassandra", "dynamodb"],
    "JSON": [],
    "XML": [],
    # BI
    "Power BI": ["powerbi", "dax"],
    "Tableau": [],
    "Looker": [],
    "Excel": ["advanced excel", "vba"],
    "Data Visualisation": ["data visualization", "dashboards", "dashboard", "dashboarding"],
    "KPI Reporting": ["kpis", "kpi"],
    # cloud / devops
    "AWS": ["amazon web services", "sagemaker", "s3", "lambda"],
    "Azure": ["microsoft azure"],
    "GCP": ["google cloud", "google cloud platform"],
    "Docker": ["containers", "containerisation", "containerization"],
    "Kubernetes": ["k8s"],
    "CI/CD": ["ci / cd", "continuous integration", "continuous delivery", "continuous deployment"],
    "Jenkins": [],
    "GitHub Actions": [],
    "GitLab CI": [],
    "Git": ["github", "gitlab"],
    "Terraform": [],
    "Linux": ["unix"],
    "MLOps": ["model deployment", "model monitoring"],
    "MLflow": [],
    # APIs / backend
    "REST APIs": ["rest", "restful", "rest api", "restful apis"],
    "SOAP": [],
    "Webhooks": ["webhook"],
    "FastAPI": [],
    "Flask": [],
    "Django": [],
    "Microservices": [],
    # testing / quality
    "Test Automation": ["automated testing", "automation testing", "test automation frameworks"],
    "Selenium": [],
    "pytest": [],
    "API Testing": ["postman"],
    # ways of working
    "Agile": ["scrum", "kanban"],
    "Stakeholder Management": ["stakeholders", "stakeholder communication"],
    "Team Leadership": ["led a team", "team lead", "mentored", "mentoring"],
    "Salesforce": [],
    "Jira": [],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


@lru_cache(maxsize=4096)
def _phrase_regex(phrase: str) -> re.Pattern[str]:
    # Word-ish boundaries that also work for terms like "C++", "CI/CD", ".net".
    return re.compile(r"(?<![a-z0-9])" + re.escape(_norm(phrase)) + r"(?![a-z0-9])")


def literal_hit(text: str, phrase: str) -> bool:
    """Exact employer wording present (case/whitespace-insensitive)? This is what an ATS checks."""
    return bool(_phrase_regex(phrase).search(_norm(text)))


def canonical(skill: str) -> str | None:
    """Map any alias or canonical spelling to its canonical name, if known."""
    n = _norm(skill)
    for canon, aliases in SKILL_ALIASES.items():
        if n == _norm(canon) or n in aliases:
            return canon
    return None


def surface_forms(skill: str) -> list[str]:
    canon = canonical(skill)
    if canon is None:
        return [skill]
    return [canon, *SKILL_ALIASES[canon]]


def _distinctive(form: str) -> bool:
    return len(form) > 2 or not form.isalnum()  # keeps "C#", drops "R", "Go", "ML"


def _usable_forms(forms: list[str]) -> list[str]:
    # One- and two-letter forms ("R", "Go", "ML", "ts") are too ambiguous to match on their own.
    usable = [f for f in forms if _distinctive(f)]
    return usable or forms


def supported(text: str, skill: str) -> bool:
    """Does the text show this skill under ANY known name? Used by the fact guard."""
    t = _norm(text)
    return any(_phrase_regex(f).search(t) for f in _usable_forms(surface_forms(skill)))


def find_skills(text: str) -> list[str]:
    """All canonical skills the text shows (alias-aware), in vocabulary order."""
    t = _norm(text)
    found = []
    for canon, aliases in SKILL_ALIASES.items():
        forms = [f for f in [canon, *aliases] if _distinctive(f)]
        if any(_phrase_regex(f).search(t) for f in forms):
            found.append(canon)
    return found
