"""Compact, agent-friendly renderings of resume payloads."""

from typing import Any

from app.schemas.models import ResumeData


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def resume_summary(fetch_data: dict[str, Any]) -> dict[str, Any]:
    """Summarize a ``GET /resumes`` payload without the full resume body."""
    processed = fetch_data.get("processed_resume") or {}
    personal = processed.get("personalInfo") or {}
    additional = processed.get("additional") or {}
    raw = fetch_data.get("raw_resume") or {}
    return {
        "resume_id": fetch_data.get("resume_id"),
        "title": fetch_data.get("title"),
        "parent_id": fetch_data.get("parent_id"),
        "is_tailored": bool(fetch_data.get("parent_id")),
        "processing_status": raw.get("processing_status"),
        "name": personal.get("name"),
        "headline": personal.get("title"),
        "summary": processed.get("summary") or None,
        "counts": {
            "work_experience": len(processed.get("workExperience") or []),
            "education": len(processed.get("education") or []),
            "projects": len(processed.get("personalProjects") or []),
            "technical_skills": len(additional.get("technicalSkills") or []),
            "custom_sections": len(processed.get("customSections") or {}),
        },
        "has_cover_letter": bool(fetch_data.get("cover_letter")),
        "has_outreach_message": bool(fetch_data.get("outreach_message")),
        "has_interview_prep": bool(fetch_data.get("interview_prep")),
    }


def _bullets(lines: list[str], items: Any) -> None:
    if isinstance(items, str):
        items = [items]
    for item in items or []:
        text = _clean(item)
        if text:
            lines.append(f"- {text}")


def resume_markdown(fetch_data: dict[str, Any]) -> str:
    """Render a ``GET /resumes`` payload as Markdown.

    Uses the structured resume when available and falls back to the stored raw
    content (the original upload's Markdown) otherwise.
    """
    processed = fetch_data.get("processed_resume")
    if not processed:
        raw = fetch_data.get("raw_resume") or {}
        return _clean(raw.get("content"))

    resume = ResumeData.model_validate(processed)
    info = resume.personalInfo
    lines: list[str] = [f"# {_clean(info.name) or 'Resume'}"]
    if _clean(info.title):
        lines.append(f"**{_clean(info.title)}**")
    contact = [
        _clean(value)
        for value in (info.email, info.phone, info.location, info.website, info.linkedin, info.github)
        if _clean(value)
    ]
    if contact:
        lines.append(" | ".join(contact))

    if _clean(resume.summary):
        lines += ["", "## Summary", _clean(resume.summary)]

    if resume.workExperience:
        lines += ["", "## Experience"]
        for job in resume.workExperience:
            heading = " - ".join(part for part in (_clean(job.title), _clean(job.company)) if part)
            lines += ["", f"### {heading}"]
            meta = " | ".join(part for part in (_clean(job.years), _clean(job.location)) if part)
            if meta:
                lines.append(meta)
            _bullets(lines, job.description)

    if resume.education:
        lines += ["", "## Education"]
        for school in resume.education:
            heading = " - ".join(
                part for part in (_clean(school.degree), _clean(school.institution)) if part
            )
            lines += ["", f"### {heading}"]
            if _clean(school.years):
                lines.append(_clean(school.years))
            if _clean(school.description):
                lines.append(_clean(school.description))

    if resume.personalProjects:
        lines += ["", "## Projects"]
        for project in resume.personalProjects:
            heading = " - ".join(part for part in (_clean(project.name), _clean(project.role)) if part)
            lines += ["", f"### {heading}"]
            if _clean(project.years):
                lines.append(_clean(project.years))
            _bullets(lines, project.description)

    additional = resume.additional.model_dump()
    labelled = (
        ("technicalSkills", "Technical Skills"),
        ("languages", "Languages"),
        ("certificationsTraining", "Certifications"),
        ("awards", "Awards"),
    )
    extra = [(label, additional.get(key) or []) for key, label in labelled]
    if any(values for _, values in extra):
        lines += ["", "## Additional"]
        for label, values in extra:
            if values:
                lines.append(f"**{label}:** " + ", ".join(_clean(v) for v in values if _clean(v)))

    display_names = {meta.key: meta.displayName for meta in resume.sectionMeta}
    for key, section in resume.customSections.items():
        section_data = section.model_dump()
        lines += ["", f"## {_clean(display_names.get(key)) or key}"]
        if _clean(section_data.get("text")):
            lines.append(_clean(section_data.get("text")))
        _bullets(lines, section_data.get("strings"))
        for item in section_data.get("items") or []:
            heading = " - ".join(
                part for part in (_clean(item.get("title")), _clean(item.get("subtitle"))) if part
            )
            if heading:
                lines += ["", f"### {heading}"]
            _bullets(lines, item.get("description"))

    return "\n".join(lines).strip() + "\n"
