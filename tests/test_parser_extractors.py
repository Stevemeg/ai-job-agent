"""Parser regression tests built from REAL parse failures.

The checkmark-bullet/no-duration layout below mirrors an actual resume that
extracted ZERO projects and reported institution = "Docker command." (the
tail of the preceding project bullet). These tests pin the fixes.
"""
from backend.resume_parser.project_extractor import extract_projects
from backend.resume_parser.education_extractor import extract_education


# Layout 3: checkmark bullets, NO duration lines (the live failure)
CHECKMARK_RESUME = """PROJECTS
Medical AI Copilot (RAG-based Clinical Assistant)
✓ Designed and deployed a Retrieval-Augmented Generation (RAG) system for clinical Q&A, grounding responses
strictly in verified medical documents to reduce hallucinations.
✓ Implemented semantic retrieval using Sentence Transformers + FAISS, indexing 2,500+ medical documents.
✓ Integrated Groq-hosted Llama 3.1 inference with a Streamlit-based frontend, enabling real-time AI-assisted
medical querying with production-style deployment architecture.
Diabetic Retinopathy Detection
✓ Built a two-stage deep learning pipeline using EfficientNetB0 for binary DR/No-DR classification (90%+ accuracy) and
MobileNetV2+CBAM for 4-level severity grading (85%+ accuracy).
✓ Applied Python OOP design for model loading, image augmentation, and prediction modules — keeping the codebase
clean and independently testable.
Credit Risk Intelligence Platform
✓ Built a production-grade credit risk platform on the Home Credit Default Risk dataset (166K+ applicants) that predicts
loan default probability.
✓ Explains decisions with SHAP, and lets analysts query applicant data in plain English — all deployed via a single
Docker command.
EDUCATION
Acharya Institute of Technology — B.E. Information Science
2022 – 2026
CGPA: 8.2
"""

# Layout 1: bullet-• with separate duration lines (the original resume shape)
DURATION_RESUME = """PROJECTS
Deep Multimodal Visual Question Answering (VQA) System LINK
Nov 2025 – Jan 2026
• Architected a multimodal transformer fusing CLIP vision embeddings.
• Achieved 33% Top-1 accuracy on the VQA v2 subset.
Synthetic Data Generation for Medical Imaging
Mar 2025 – Aug 2025
• Engineered GAN-based pipelines generating 10,000+ synthetic images.
EDUCATION
Acharya Institute of Technology
B.E. in Artificial Intelligence and Machine Learning | CGPA: 7.65
2022 - 2026
"""


class TestCheckmarkNoDurationLayout:
    def test_all_three_projects_extracted(self):
        projects = extract_projects(CHECKMARK_RESUME)
        titles = [p["title"] for p in projects]
        assert titles == [
            "Medical AI Copilot (RAG-based Clinical Assistant)",
            "Diabetic Retinopathy Detection",
            "Credit Risk Intelligence Platform",
        ]

    def test_bullets_and_wrapped_lines_assigned_correctly(self):
        projects = extract_projects(CHECKMARK_RESUME)
        copilot, retino, credit = projects
        assert "FAISS" in copilot["description"]
        assert "deployment architecture" in copilot["description"]   # wrap joined
        assert "EfficientNetB0" in retino["description"]
        assert "SHAP" in credit["description"]
        # no cross-project bleed
        assert "SHAP" not in copilot["description"]
        assert "FAISS" not in retino["description"]

    def test_bullet_separators_preserved(self):
        copilot = extract_projects(CHECKMARK_RESUME)[0]
        assert copilot["description"].count("•") >= 3     # downstream splits on •

    def test_education_not_garbage(self):
        edu = extract_education(CHECKMARK_RESUME)
        assert edu, "education must be found"
        e = edu[0]
        assert e["college"] == "Acharya Institute of Technology"
        assert "Docker" not in (e["college"] or "")       # the live bug
        assert "B.E" in e["degree"]
        assert e["cgpa"] == "8.2"
        assert e["years"] == "2022 - 2026"


class TestDurationLayoutRegression:
    def test_projects_with_duration_lines_still_work(self):
        projects = extract_projects(DURATION_RESUME)
        assert [p["title"] for p in projects] == [
            "Deep Multimodal Visual Question Answering (VQA) System",
            "Synthetic Data Generation for Medical Imaging",
        ]
        assert projects[0]["duration"].startswith("Nov 2025")
        assert "CLIP" in projects[0]["description"]
        assert "GAN" in projects[1]["description"]

    def test_education_neighbor_scan_still_works(self):
        e = extract_education(DURATION_RESUME)[0]
        assert e["college"] == "Acharya Institute of Technology"
        assert e["cgpa"] == "7.65"


class TestEducationGuards:
    def test_never_grabs_non_institution_prose(self):
        text = "random prose line\nsomething else.\nB.Tech in Mechanical\n2020 - 2024"
        e = extract_education(text)[0]
        assert e["college"] is None                       # honest None > garbage

    def test_section_header_never_becomes_college(self):
        text = "EDUCATION\nB.E. Computer Science\nSome University\n2019 - 2023"
        e = extract_education(text)[0]
        assert e["college"] == "Some University"
