"""The starter skill-name table (app/skill_seed.py): its shape, that different technologies are never merged, that the roles we researched are
covered, and that the 'implies' rules (a category is satisfied by its tools) line up with the table."""
import json

import pytest

from app.database import SEED_ALIASES
from app.llm.scoring import IMPLIED_BY, skill_match
from app.llm.skill_canonicalizer import canonicalize_skills, known_names_for
from app.models import CandidateProfile, JobProfile
from app.skill_seed import ALIAS_GROUPS, build_seed

# ---------------------------------------------------------------- shape
def test_the_table_is_large_and_every_key_is_clean():
    assert len(ALIAS_GROUPS) >= 750 and len(SEED_ALIASES) >= 4300                 # catches an accidental truncation
    for key, canonical in SEED_ALIASES.items():
        assert key == key.strip().lower() and key, repr(key)
        assert canonical == canonical.strip() and 0 < len(canonical) <= 40, canonical      # the canonicalizer refuses longer names
        assert "," not in canonical and "\n" not in canonical


def test_every_canonical_name_resolves_to_itself():
    for canonical in ALIAS_GROUPS:
        assert SEED_ALIASES[canonical.lower()] == canonical, canonical


def test_canonical_names_differ_by_more_than_capitalisation():
    seen = {}
    for canonical in ALIAS_GROUPS:
        low = canonical.lower()
        assert low not in seen, f"{canonical!r} and {seen[low]!r} are the same name"
        seen[low] = canonical


def test_a_spelling_claimed_by_two_skills_is_refused():
    with pytest.raises(ValueError, match="claimed by both"):
        build_seed({"Alpha": ["shared"], "Beta": ["shared"]})
    with pytest.raises(ValueError):
        build_seed({"Alpha": [" "]})
    assert build_seed({"Alpha": ["a1", "A2"]}) == {"alpha": "Alpha", "a1": "Alpha", "a2": "Alpha"}


def test_the_database_loads_this_exact_table():
    from app import database
    assert database.SEED_ALIASES is SEED_ALIASES


# ---------------------------------------------------------------- nothing that already worked changed
ORIGINAL_SEED = {
    "ml": "Machine Learning", "machine learning": "Machine Learning", "py": "Python", "python3": "Python", "js": "JavaScript", "fast api": "FastAPI",
    "fastapi": "FastAPI", "tf": "TensorFlow", "tensorflow": "TensorFlow", "docker": "Docker", "postgres": "PostgreSQL", "postgresql": "PostgreSQL",
    "sklearn": "Scikit-learn", "scikit learn": "Scikit-learn", "sql": "SQL", "numpy": "NumPy", "pandas": "Pandas", "opencv": "OpenCV", "yolo": "YOLO",
    "lstm": "LSTM", "cnn": "CNN", "cnns": "CNN", "llm": "LLMs", "llms": "LLMs", "large language models": "LLMs", "rag": "RAG",
    "retrieval-augmented generation": "RAG", "github": "Git/GitHub", "git": "Git/GitHub", "git/github": "Git/GitHub", "typescript": "TypeScript",
    "javascript": "JavaScript", "nextjs": "Next.js", "next.js": "Next.js", "react.js": "React", "power bi": "Power BI", "aws": "AWS", "k8s": "Kubernetes",
    "scikit-learn": "Scikit-learn",
}


def test_the_original_39_entries_mean_exactly_what_they_did():
    assert {k: SEED_ALIASES.get(k) for k in ORIGINAL_SEED} == ORIGINAL_SEED


# ---------------------------------------------------------------- ambiguous words that must NOT be skill spellings
AMBIGUOUS = ["cv", "gtm", "pm", "be", "can", "ds", "pip", "er", "od", "dash", "his", "next", "monday", "var", "ga", "sit", "ado", "stm", "lora", "csm",
             "ps", "word", "linear"]


@pytest.mark.parametrize("word", AMBIGUOUS)
def test_ambiguous_words_are_left_out(word):
    assert word not in SEED_ALIASES, word


# ---------------------------------------------------------------- different things are never merged
NEVER_THE_SAME = [
    ["java", "javascript"], ["c", "c++", "c#"], ["react", "react native"], ["react", "redux", "next.js"], ["sql", "mysql", "postgresql", "sql server", "sqlite", "mongodb"],
    ["angular", "angularjs"], ["vue.js", "nuxt.js"], ["node.js", "express.js", "nestjs"], ["aws", "ec2", "s3", "lambda", "amazon ecs", "amazon eks", "aws fargate"],
    ["docker", "kubernetes", "docker compose", "helm"], ["jenkins", "github actions", "gitlab ci", "circleci"], ["tensorflow", "keras", "pytorch"],
    ["power bi", "tableau", "looker", "looker studio", "qlik"], ["jira", "confluence", "trello", "asana"], ["selenium", "cypress", "playwright", "appium"],
    ["gdpr", "hipaa", "iso 27001", "soc 2", "pci dss"], ["workday", "bamboohr", "darwinbox", "sap successfactors"], ["greenhouse", "lever", "taleo"],
    ["ecg interpretation", "hemodynamic monitoring", "ventilator management", "arterial line care"], ["ppc advertising", "google ads", "meta ads manager"],
    ["machine learning", "deep learning", "neural networks", "natural language processing", "computer vision"], ["advanced excel", "excel vba"],
    ["rest api design", "graphql", "grpc"], ["firebase", "firestore"], ["looker", "looker studio"], ["terraform", "ansible", "puppet", "chef"],
    ["prometheus", "grafana", "datadog"], ["salesforce", "hubspot", "zoho crm"], ["sap", "netsuite", "microsoft dynamics", "tally"],
    ["bls certification", "acls certification", "pals certification", "cpr certification"], ["bsn degree", "rn license"], ["spring", "spring boot"],
    ["django", "django rest framework", "flask", "fastapi"], ["pytest", "junit", "testng"], ["owasp", "penetration testing", "siem"],
    ["bluetooth", "wi-fi", "zigbee", "lorawan"], ["bookkeeping", "cost accounting", "accounting principles"],
    ["equity research", "investment banking", "private equity experience", "portfolio management"], ["forecasting", "budgeting", "variance analysis"],
    ["kafka", "rabbitmq"], ["xgboost", "lightgbm", "catboost"], ["cnn", "rnn", "lstm", "gru", "gan"], ["swift", "kotlin", "dart"], ["android development", "ios development"],
]


@pytest.mark.parametrize("group", NEVER_THE_SAME)
def test_related_but_different_skills_stay_separate(group):
    resolved = [SEED_ALIASES.get(g) for g in group]
    assert all(resolved), {g: r for g, r in zip(group, resolved)}
    assert len(set(resolved)) == len(group), dict(zip(group, resolved))


# ---------------------------------------------------------------- the spellings in the user's own example, and the roles we researched
def test_computer_vision_in_every_spelling():
    for spelling in ["Computer Vision", "computervision", "ComputerVision", "computer-vision", "Computer-Vision", "machine vision"]:
        assert SEED_ALIASES[spelling.lower()] == "Computer Vision", spelling


ROLE_SKILLS = {
    "Product Manager / PM intern": ["Product Roadmap", "roadmapping", "user stories", "PRD", "product requirements document", "backlog grooming", "sprint planning",
                                    "Jira", "Confluence", "Trello", "Figma", "wireframes", "A/B testing", "RICE", "MoSCoW", "go-to-market", "GTM strategy",
                                    "user research", "market research", "competitor analysis", "OKRs", "stakeholder management", "SQL", "Mixpanel", "Amplitude",
                                    "product analytics", "Google Analytics", "Agile", "Scrum", "Kanban", "MVP", "customer discovery", "pricing strategy"],
    "Backend intern": ["Java", "Spring Boot", "Node.js", "Express", "NestJS", "Django", "Flask", "FastAPI", "REST APIs", "GraphQL", "PostgreSQL", "MySQL",
                       "MongoDB", "Redis", "Kafka", "RabbitMQ", "Docker", "microservices", "JWT", "OAuth2", "unit testing", "JUnit", "Mockito", "pytest", "Git",
                       "Postman", "Swagger", "SQL", "data structures and algorithms", "OOP", "multithreading", "exception handling", "Hibernate", "JPA"],
    "DevOps intern": ["Linux", "Bash", "shell scripting", "Git", "GitHub Actions", "Jenkins", "GitLab CI", "CI/CD", "Docker", "Kubernetes", "Helm", "Terraform",
                      "Ansible", "AWS", "Azure", "GCP", "Prometheus", "Grafana", "ELK", "Nginx", "monitoring", "infrastructure as code", "ArgoCD", "networking", "DevOps"],
    "Data analyst": ["SQL", "Python", "R", "Tableau", "Power BI", "Excel", "pivot tables", "VLOOKUP", "data cleaning", "dashboards", "KPI reporting", "A/B testing",
                     "Google Sheets", "statistics", "data visualization", "EDA", "pandas", "NumPy", "Looker Studio", "BigQuery"],
    "Data / ML engineer": ["Airflow", "Spark", "PySpark", "Kafka", "dbt", "Snowflake", "BigQuery", "Redshift", "ETL", "data warehouse", "Hadoop", "Databricks",
                           "TensorFlow", "PyTorch", "scikit-learn", "XGBoost", "MLOps", "MLflow", "feature engineering", "NLP", "computer vision", "OpenCV", "YOLO",
                           "LLMs", "RAG", "LangChain", "Hugging Face", "prompt engineering", "vector database", "embeddings", "Streamlit", "model deployment"],
    "Frontend / mobile": ["HTML5", "CSS3", "JavaScript", "TypeScript", "React", "Redux", "Next.js", "Vue", "Angular", "Tailwind", "Bootstrap", "Sass", "Webpack",
                          "responsive design", "WCAG", "Android", "Kotlin", "Swift", "SwiftUI", "Jetpack Compose", "Flutter", "React Native", "Xcode", "Firebase"],
    "QA / testing": ["manual testing", "automation testing", "Selenium", "Cypress", "Playwright", "Appium", "Postman", "JMeter", "TestNG", "Cucumber", "BDD",
                     "API testing", "regression testing", "test cases", "test plan", "bug tracking", "STLC", "SDLC", "UAT", "smoke testing", "performance testing"],
    "UI/UX design": ["UI/UX", "Figma", "Adobe XD", "wireframing", "prototyping", "user research", "usability testing", "design systems", "Photoshop", "Illustrator",
                     "Canva", "interaction design", "customer journey mapping"],
    "Security": ["OWASP", "penetration testing", "SIEM", "Splunk", "Wireshark", "Nmap", "Burp Suite", "Kali Linux", "vulnerability assessment", "incident response",
                 "cryptography", "IAM", "GDPR", "ISO 27001", "NIST", "SOC", "firewalls"],
    "Embedded / electronics": ["Embedded C", "Arduino", "Raspberry Pi", "ESP32", "STM32", "RTOS", "FreeRTOS", "IoT", "MQTT", "I2C", "SPI", "UART", "CAN bus", "Verilog",
                               "VHDL", "FPGA", "VLSI", "PCB design", "Altium", "MATLAB", "Simulink", "LabVIEW", "DSP", "microcontrollers"],
    "Marketing / sales": ["SEO", "SEM", "Google Ads", "PPC", "Meta Ads", "Facebook Ads", "Mailchimp", "HubSpot", "Salesforce", "CRM", "Google Analytics", "SEMrush",
                          "Ahrefs", "content marketing", "email marketing", "social media marketing", "copywriting", "lead generation", "cold calling", "B2B sales"],
    "Finance / HR / healthcare": ["financial modeling", "DCF", "valuation", "GAAP", "IFRS", "Tally", "SAP", "GST", "FP&A", "recruitment", "talent acquisition",
                                  "payroll", "HRIS", "onboarding", "labour law", "BLS", "ACLS", "EMR", "infection control", "ICU", "medication administration"],
}


@pytest.mark.parametrize("role", list(ROLE_SKILLS))
def test_the_skills_of_each_researched_role_are_known(role):
    missing = [s for s in ROLE_SKILLS[role] if s.lower() not in SEED_ALIASES]
    assert not missing, f"{role}: no entry for {missing}"


def test_a_typical_pm_intern_resume_needs_no_ai_call():
    skills = ["Product Roadmap", "User Stories", "JIRA", "Figma", "SQL", "A/B Testing", "Market Research", "Agile", "Google Analytics", "Competitor Analysis"]

    def llm(system, user):
        raise AssertionError("the AI should not be asked: every skill is already known")

    mapping, learned = canonicalize_skills(skills, SEED_ALIASES, llm)
    assert learned == {} and mapping["JIRA"] == "Jira" and mapping["Competitor Analysis"] == "Competitive Analysis" and mapping["Agile"] == "Agile Methodology"


# ---------------------------------------------------------------- 'implies' rules line up with the table
def test_every_umbrella_and_tool_in_the_implies_rules_is_a_known_skill():
    problems = []
    for umbrella, tools in IMPLIED_BY.items():
        if umbrella not in SEED_ALIASES:
            problems.append(("umbrella", umbrella))
        problems += [("tool", umbrella, t) for t in tools if t not in SEED_ALIASES]
    assert not problems, problems


def _status(have, need, extra=()):
    prof = CandidateProfile(name="x", skills=list(have))
    job = JobProfile(title="t", required_skills=list(need))
    _, _, rows = skill_match(prof, job, SEED_ALIASES)
    return {r.skill: r.status for r in rows}


@pytest.mark.parametrize("have,need", [
    (["Scrum"], "Agile Methodology"), (["Jenkins"], "CI/CD Pipelines"), (["Workday"], "HRIS Platforms"), (["AWS"], "Cloud Computing"),
    (["GitHub"], "Version Control"), (["Docker"], "Containerization"), (["Tableau"], "Data Visualization"), (["Spring Boot"], "Java"),
    (["BERT"], "Natural Language Processing"), (["Selenium"], "Software Testing"), (["Salesforce"], "CRM"), (["Terraform"], "Infrastructure as Code"),
    (["Prometheus"], "Monitoring & Logging"), (["Figma"], "UI/UX Design"), (["User Stories"], "Product Management"), (["Greenhouse"], "Applicant Tracking Systems"),
])
def test_owning_a_tool_gives_partial_credit_for_its_category(have, need):
    assert _status(have, [need])[need] == "inferred"


@pytest.mark.parametrize("have,need", [
    (["Workday"], "BambooHR"), (["GDPR"], "HIPAA"), (["Java"], "JavaScript"), (["Jenkins"], "GitHub Actions"), (["Amazon ECS"], "Amazon EKS"),
    (["MySQL"], "PostgreSQL"), (["React"], "React Native"), (["Cypress"], "Selenium"),
])
def test_a_different_tool_is_not_credited(have, need):
    assert _status(have, [need])[need] == "missing"


# ---------------------------------------------------------------- the names shown to the AI
def test_relevant_known_names_are_shown_even_when_the_table_is_large():
    assert len(set(SEED_ALIASES.values())) > 600
    shown = known_names_for(["kubernetes admin", "terraform modules"], SEED_ALIASES)
    assert len(shown) == 120 and "Kubernetes" in shown and "Terraform" in shown                  # 'K' and 'T' are far past the first 120 alphabetically
    assert shown[:2] and all(isinstance(n, str) for n in shown)


def test_a_small_table_is_shown_in_full_and_sorted():
    aliases = {"a": "Zed", "b": "Alpha", "c": "Mid"}
    assert known_names_for(["x"], aliases) == ["Alpha", "Mid", "Zed"]


def test_the_prompt_the_ai_receives_contains_the_relevant_names():
    seen = {}

    def llm(system, user):
        seen["user"] = user
        return json.dumps({"mapping": {"Quantum computing basics": "Quantum Computing"}})

    mapping, learned = canonicalize_skills(["Quantum computing basics"], SEED_ALIASES, llm)
    assert mapping["Quantum computing basics"] == "Quantum Computing" and "quantum computing" in learned
    assert "KNOWN:" in seen["user"]

# real requirements that jobs in the old database asked for (found missing, then added); degrees are handled in code, not here
REAL_JOB_REQUIREMENTS = ["Maven", "Gradle", "IntelliJ IDEA", "Eclipse IDE", "System Design", "Team Management", "MBA", "SaaS Experience", "CCRN Certification",
                         "SHRM-CP Certification", "Hybrid Retrieval (BM25 + Embeddings)", "n8n", "Window Functions", "SQL Injection", "Stripe", "Tesseract", "OCR"]


def test_requirements_seen_in_real_jobs_are_known():
    assert [r for r in REAL_JOB_REQUIREMENTS if r.lower() not in SEED_ALIASES] == []
    assert SEED_ALIASES["intellij"] == "IntelliJ IDEA" and SEED_ALIASES["vs code"] == "Visual Studio Code" and SEED_ALIASES["ctes"] == "CTEs"
