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
    "SQL": [],
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
    "TensorFlow": ["tf"],
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
    "Time Series": ["time-series", "time series forecasting"],
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
    "Vector Databases": ["vector database", "vector db", "vector store", "vector stores"],
    "Embeddings": ["vector embeddings", "text embeddings", "word embeddings"],  # not "embedding" (a verb)
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
    "NoSQL": ["non-relational databases"],
    "JSON": [],
    "XML": [],
    # BI
    "Power BI": ["powerbi"],
    "Tableau": [],
    "Looker": [],
    "Excel": ["advanced excel"],
    "Data Visualisation": ["data visualization", "dashboards", "dashboard", "dashboarding"],
    "KPI Reporting": ["kpis", "kpi"],
    # cloud / devops
    "AWS": ["amazon web services"],
    "Azure": ["microsoft azure"],
    "GCP": ["google cloud", "google cloud platform"],
    "Docker": ["containers", "containerisation", "containerization"],
    "Kubernetes": ["k8s"],
    "CI/CD": ["ci / cd", "continuous integration", "continuous delivery", "continuous deployment"],
    "Jenkins": [],
    "GitHub Actions": [],
    "GitLab CI": [],
    "Git": [],
    "Terraform": [],
    "Linux": ["unix"],
    "MLOps": [],
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
    "API Testing": [],
    # ways of working
    "Agile": ["agile delivery"],
    "Stakeholder Management": ["stakeholders", "stakeholder communication"],
    "Team Leadership": ["led a team", "team lead", "mentored", "mentoring"],
    "Salesforce": [],
    "Jira": [],
}


# Specific products and practices. Each implies its parent (PostgreSQL -> SQL), but a parent never
# implies a child (SQL -/-> PostgreSQL) and siblings never imply each other (SQL Server -/-> PostgreSQL).
SKILL_CHILDREN: dict[str, dict[str, list[str]]] = {
    "SQL": {"PostgreSQL": ["postgres"], "MySQL": [], "SQL Server": ["mssql", "microsoft sql server"],
            "SQLite": [], "T-SQL": [], "PL/SQL": [], "Oracle Database": ["oracle db"]},
    "NoSQL": {"MongoDB": [], "Cassandra": [], "DynamoDB": [], "Redis": [], "Elasticsearch": []},
    "Vector Databases": {"Pinecone": [], "FAISS": [], "Chroma": ["chromadb"], "pgvector": [], "Weaviate": [], "Qdrant": []},
    "AWS": {"SageMaker": ["amazon sagemaker"], "S3": ["amazon s3"], "AWS Lambda": [], "EC2": [], "Glue": ["aws glue"]},
    "Azure": {"Azure Data Factory": ["adf"], "Azure Synapse": ["synapse"], "Azure ML": ["azure machine learning"]},
    "GCP": {"Vertex AI": [], "Dataflow": []},
    "Power BI": {"DAX": [], "Power Query": []},
    "Excel": {"VBA": [], "Power Pivot": []},
    "Git": {"GitHub": [], "GitLab": [], "Bitbucket": []},
    "Agile": {"Scrum": [], "Kanban": []},
    "API Testing": {"Postman": []},
    "TensorFlow": {"Keras": []},
    "MLOps": {"Model Deployment": [], "Model Monitoring": [], "Feature Store": ["feature stores"]},
    "Time Series": {"Forecasting": []},
}
PARENT: dict[str, str] = {}
for _parent, _kids in SKILL_CHILDREN.items():
    for _kid, _syn in _kids.items():
        SKILL_ALIASES[_kid] = _syn
        PARENT[_kid] = _parent


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
    """Names this exact skill goes by (synonyms only: no children, no parent)."""
    canon = canonical(skill)
    if canon is None:
        return [skill]
    return [canon, *SKILL_ALIASES[canon]]


def _descendants(canon: str) -> list[str]:
    out = []
    for kid, parent in PARENT.items():
        if parent == canon:
            out += [kid, *_descendants(kid)]
    return out


def support_forms(skill: str) -> list[str]:
    """Everything that proves a skill: its own names plus its children's (S3 proves AWS; AWS doesn't prove S3)."""
    canon = canonical(skill)
    if canon is None:
        return [skill]
    forms = surface_forms(canon)
    for kid in _descendants(canon):
        forms += surface_forms(kid)
    return forms


def _distinctive(form: str) -> bool:
    return len(form) > 2 or not form.isalpha()  # keeps "C#", "S3"; drops "R", "Go", "ML"


def _usable_forms(forms: list[str]) -> list[str]:
    # One- and two-letter forms ("R", "Go", "ML", "ts") are too ambiguous to match on their own.
    usable = [f for f in forms if _distinctive(f)]
    return usable or forms


def supported(text: str, skill: str) -> bool:
    """Does the text show this skill under ANY known name? Used by the fact guard."""
    t = _norm(text)
    return any(_phrase_regex(f).search(t) for f in _usable_forms(support_forms(skill)))


def find_skills(text: str) -> list[str]:
    """Every known skill the text names directly (synonym-aware), including specific products."""
    t = _norm(text)
    found = []
    for canon, aliases in SKILL_ALIASES.items():
        forms = [f for f in [canon, *aliases] if _distinctive(f)]
        if any(_phrase_regex(f).search(t) for f in forms):
            found.append(canon)
    return found
