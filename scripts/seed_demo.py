#!/usr/bin/env python3
"""生成并导入示例论文，用于演示/录视频。

用法：
    cd /Users/zc/Desktop/个人知识库
    env -u PYTHONPATH backend/venv/bin/python scripts/seed_demo.py

说明：
- 生成一篇与结直肠癌 T 分期相关的英文示例论文 PDF（避免中文字体依赖）。
- 调用后端 /api/papers/import 接口导入。
- 可选等待后台处理完成，方便录视频时直接展示已处理状态。
"""

import io
import time
import sys
from pathlib import Path

import requests
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_CENTER


def make_demo_pdf() -> bytes:
    """生成示例 PDF 的二进制内容。"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=72,
        leftMargin=72,
        topMargin=72,
        bottomMargin=18,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DemoTitle",
        parent=styles["Title"],
        fontSize=18,
        leading=24,
        alignment=TA_CENTER,
        spaceAfter=20,
    )
    heading_style = ParagraphStyle(
        "DemoHeading",
        parent=styles["Heading2"],
        fontSize=14,
        leading=20,
        spaceAfter=10,
        spaceBefore=14,
    )
    body_style = ParagraphStyle(
        "DemoBody",
        parent=styles["BodyText"],
        fontSize=11,
        leading=16,
        alignment=TA_LEFT,
        spaceAfter=8,
    )

    story = []
    story.append(Paragraph(
        "ReCo-MIL: Recurrent Consistency-aware Multiple Instance Learning for "
        "Colorectal Cancer T-Stage Prediction from Whole Slide Images",
        title_style,
    ))
    story.append(Paragraph(
        "<b>Authors:</b> Demo Author, Example Collaborator, Test Researcher<br/>"
        "<b>Affiliation:</b> Huzhou University, School of Information Engineering<br/>"
        "<b>Year:</b> 2026",
        body_style,
    ))
    story.append(Spacer(1, 0.2 * inch))

    sections = [
        (
            "Abstract",
            "Accurate T-stage prediction is essential for treatment planning in colorectal cancer. "
            "Whole slide images (WSIs) contain rich histological information but are gigapixel-sized, "
            "making direct deep learning infeasible. Multiple instance learning (MIL) treats each WSI as "
            "a bag of instance patches and has become a standard paradigm. However, conventional MIL ignores "
            "inter-instance consistency and the recurrent spatial dependencies among tumor regions. We propose "
            "ReCo-MIL, a recurrent consistency-aware MIL framework that integrates a consistency regularizer "
            "with a gated recurrent aggregator. We also introduce CAFR-MIL, a cross-attention feature refinement "
            "module to suppress noisy background instances. Extensive experiments on an in-house colorectal "
            "dataset show that ReCo-MIL achieves 87.3% accuracy and an AUC of 0.914, outperforming Attention- "
            "and Transformer-based MIL baselines. Ablation studies confirm that both the consistency loss and "
            "the CAFR module contribute to performance gains. The proposed method provides a lightweight yet "
            "effective solution for clinical T-stage estimation.",
        ),
        (
            "1. Introduction",
            "Colorectal cancer is one of the most common malignant tumors worldwide. T-stage classification, "
            "which describes the depth of tumor invasion into the bowel wall, is a critical factor in treatment "
            "decision-making. Traditional staging relies on pathologists' visual examination of hematoxylin and "
            "eosin (H&E) stained WSIs. This process is time-consuming and suffers from inter-observer variability. "
            "Recent advances in computational pathology have demonstrated that MIL can predict slide-level labels "
            "from instance-level features without pixel-wise annotations. Despite these successes, existing MIL methods "
            "often treat instances as independent samples and fail to explicitly model the recurrent progression of "
            "tumor regions across patches. This work addresses the above limitations by proposing ReCo-MIL.",
        ),
        (
            "2. Related Work",
            "Ilse et al. introduced attention-based MIL for histopathology. Transformer-based approaches such as "
            "TransMIL and DTFD-MIL further improved feature aggregation. However, these models primarily focus on "
            "single-instance attention weights and ignore pairwise consistency. Consistency regularization has been "
            "successfully applied in semi-supervised learning but remains under-explored in MIL. Our method unifies "
            "consistency-aware instance selection with a recurrent aggregator for WSI classification.",
        ),
        (
            "3. Method",
            "The proposed framework consists of three stages. First, each WSI is tiled into non-overlapping patches "
            "at 20x magnification and encoded by a pre-trained ResNet-50 to produce 2048-dimensional feature vectors. "
            "Second, the Cross-Attention Feature Refinement (CAFR) module computes pairwise attention between every "
            "instance and a learnable class prototype, suppressing background patches while enhancing tumor-related ones. "
            "Third, the recurrent aggregator processes the refined feature sequence with a bidirectional Gated Recurrent "
            "Unit (BiGRU). A consistency loss is added to encourage similar predictions for neighboring instances. "
            "The slide-level probability is obtained by applying a sigmoid over the final aggregated representation.",
        ),
        (
            "4. Experiments",
            "We evaluate ReCo-MIL on a dataset of 512 colorectal WSIs collected from Huzhou University Affiliated Hospital. "
            "The dataset is divided into training (70%), validation (10%), and test (20%) sets at the patient level. "
            "We compare against AttentionMIL, CLAM, TransMIL, and DTFD-MIL. The Adam optimizer is used with an initial "
            "learning rate of 1e-4 and a cosine decay schedule. Each experiment is repeated three times and we report "
            "the mean and standard deviation. Evaluation metrics include accuracy, macro F1-score, AUC, and quadratic "
            "weighted kappa for ordinal T-stage classification.",
        ),
        (
            "5. Results",
            "ReCo-MIL achieves 87.3% accuracy, 0.847 F1, and 0.914 AUC on the test set. AttentionMIL obtains 81.2% "
            "accuracy and 0.872 AUC, while TransMIL reaches 84.6% accuracy and 0.891 AUC. The ablation study shows that "
            "removing the CAFR module drops accuracy by 2.4%, and removing the consistency loss drops accuracy by 1.8%. "
            "Combining both components yields the best performance. The confusion matrix indicates that most errors occur "
            "between adjacent T-stages, which is clinically reasonable.",
        ),
        (
            "6. Conclusion",
            "We present ReCo-MIL, a recurrent consistency-aware MIL framework for colorectal WSI T-stage prediction. "
            "The CAFR module effectively filters background noise, and the consistency loss stabilizes instance predictions. "
            "Experimental results demonstrate state-of-the-art performance on an in-house dataset. Future work will explore "
            "foundation-model-based feature extraction and multi-center validation to further improve generalizability.",
        ),
    ]

    for title, text in sections:
        story.append(Paragraph(title, heading_style))
        story.append(Paragraph(text, body_style))
        if title == "Abstract":
            story.append(Spacer(1, 0.2 * inch))

    doc.build(story)
    return buffer.getvalue()


def main():
    project_root = Path(__file__).resolve().parents[1]
    papers_dir = project_root / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = papers_dir / "demo-paper.pdf"
    pdf_bytes = make_demo_pdf()
    pdf_path.write_bytes(pdf_bytes)
    print(f"[demo] 已生成示例论文: {pdf_path}")

    # 导入论文
    # 关闭 trust_env，避免 macOS 系统代理影响直连本地服务
    session = requests.Session()
    session.trust_env = False

    api_base = "http://127.0.0.1:8000"
    health = session.get(f"{api_base}/api/health", timeout=10)
    health.raise_for_status()
    print(f"[demo] 后端健康检查: {health.text}")

    with pdf_path.open("rb") as f:
        resp = session.post(
            f"{api_base}/api/papers/import",
            files={"files": (pdf_path.name, f, "application/pdf")},
            timeout=30,
        )
    resp.raise_for_status()
    data = resp.json()
    paper_id = data["items"][0]["id"]
    print(f"[demo] 导入成功, paper_id={paper_id}, title={data['items'][0]['title']}")

    # 等待后台处理完成（最多 90 秒）
    print("[demo] 等待向量化处理...")
    for i in range(90):
        p = session.get(f"{api_base}/api/papers/{paper_id}", timeout=10)
        p.raise_for_status()
        status = p.json()["processed"]
        if status == "done":
            print(f"[demo] 处理完成 (done)")
            break
        if status == "error":
            print(f"[demo] 处理出错，请检查后端日志")
            sys.exit(1)
        time.sleep(1)
    else:
        print("[demo] 处理超时，可能是模型首次加载较慢，请稍后刷新 UI 查看")

    print(f"[demo] 可以打开 http://localhost:5173/ 查看演示")


if __name__ == "__main__":
    main()
