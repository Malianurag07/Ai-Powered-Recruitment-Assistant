"""A skill counts as 'on the resume' only as a whole word (spacing and punctuation may differ), never inside another word."""
import pytest

from app.database import SEED_ALIASES
from app.llm.verification import skill_in_text

AL = SEED_ALIASES


@pytest.mark.parametrize("skill,text", [
    ("RAG", "Built RAG pipelines for search"), ("RAG", "retrieval-augmented generation system"), ("Java", "Java, Spring Boot"),
    ("Scikit-learn", "used scikit learn and sklearn"), ("Scikit-learn", "Scikitlearn models"), ("Node.js", "Node JS and Express"),
    ("Node.js", "backend in nodejs"), ("CI/CD Pipelines", "set up CI CD for the team"), ("CI/CD Pipelines", "continuous integration with Jenkins"),
    ("Computer Vision", "Computer-Vision projects"), ("Computer Vision", "ComputerVision experiments"), ("C", "Languages: C, Python"),
    ("C++", "C/C++ and Rust"), ("C#", "C# and .NET"), ("iOS Development", "built iOS apps"), ("Advanced Excel", "Excel pivot tables"),
    ("Git/GitHub", "Git and GitHub"), ("SQL", "SQL queries"), ("Machine Learning", "ML models"), ("Natural Language Processing", "NLP"),
])
def test_real_mentions_are_found(skill, text):
    assert skill_in_text(skill, text, AL) is True


@pytest.mark.parametrize("skill,text", [
    ("RAG", "excellent storage and fragment handling"),            # 'rag' hides inside storage, fragment
    ("iOS Development", "worked in recording studios and curious"),  # 'ios' hides inside studios, curious
    ("Java", "I know JavaScript and TypeScript"),                    # Java is not JavaScript
    ("C", "I write C++ and C# daily"),                               # C is not C++ or C#
    ("Advanced Excel", "excellent communication, excelled at sales"),  # 'excel' hides inside excellent, excelled
    ("Git/GitHub", "digital gadgets and gitless workflows"),         # 'git' hides inside digital
    ("SQL", "NoSQLish thinking"),                                    # not a whole word
    ("Go", "good going gone"),                                       # 'go' hides inside good, going, gone
    ("AWS", "laws and flaws"),                                       # 'aws' hides inside laws
    ("Linux", "my favourite linuxy thing"),                          # not a whole word
    ("Machine Learning", "the machinery was learning-adjacent"),     # words apart
    ("Docker", "a dockerless setup"),
])
def test_words_hidden_inside_other_words_do_not_count(skill, text):
    assert skill_in_text(skill, text, AL) is False


def test_negation_still_applies_with_the_new_matching():
    assert skill_in_text("Docker", "no experience with Docker", AL) is False
    assert skill_in_text("Docker", "Docker and Kubernetes", AL) is True
    assert skill_in_text("Advanced Excel", "currently learning Excel", AL) is False


def test_without_a_seed_table_a_skill_is_still_found_by_its_own_name():
    assert skill_in_text("Docker", "Deployed with Docker.", {}) is True
    assert skill_in_text("Docker", "no mention here", {}) is False
