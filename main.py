"""
main.py
End-to-end orchestrator for qa-freelance-agent.

Executes the complete pipeline:
1. Loads ground truth base CV (prioritizes data/cv_base.md).
2. Fetches real QA jobs from open remote sources (Remotive, We Work Remotely, Arbeitnow, Custom RSS).
3. Deduplicates against data/processed_jobs.json to prevent repetitive work.
4. Evaluates viabilidad & down-leveling with Gemini (gemini_evaluator.py).
5. If viable, compiles tailored_cv_markdown into output/tailored_cv_{job_id}.pdf.
6. Sends rich Telegram alert with match reason, cover letter, apply URL, and PDF attached.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import ssl_setup  # noqa: F401  (trust the OS certificate store; must precede network imports)
from dotenv import load_dotenv

import gemini_evaluator
import telegram_notifier
from job_fetcher import JobHistoryManager, JobOpportunity, fetch_all_new_qa_jobs
from pdf_generator import generate_pdf_from_markdown
from prefilter import quick_reject_reason

# Load environment variables
load_dotenv()

# Outcomes of processing a single job
OUTCOME_VIABLE = "viable"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"  # Gemini/Telegram failure: do NOT mark as processed, retry next run


# Ensure UTF-8 output on Windows console
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_base_cv(file_path: Path | str = "data/cv_base.md") -> str:
    """
    Safely reads the ground-truth base CV from disk.
    Prioritizes data/cv_base.md, falling back to base_cv.md.
    """
    path = Path(file_path).resolve()
    if not path.exists():
        fallback = Path("base_cv.md").resolve()
        if fallback.exists():
            print(f"[*] Archivo {path.name} no encontrado. Usando fallback: {fallback}")
            path = fallback
        else:
            raise FileNotFoundError(
                f"[ERROR] No se pudo encontrar el CV base en {path} ni en {fallback}"
            )

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"[ERROR] El archivo CV base en {path} esta vacio.")

    return content


def extract_job_metadata(job_description: str) -> dict[str, str]:
    """Extracts job title, company, and apply URL if present in the text."""
    title_match = re.search(r"(?:Job Title|Puesto|Title):\s*(.+)", job_description, re.IGNORECASE)
    company_match = re.search(r"(?:Company|Cliente|Client):\s*(.+)", job_description, re.IGNORECASE)
    url_match = re.search(r"(?:Apply URL|Link|URL):\s*(https?://[^\s]+)", job_description, re.IGNORECASE)

    return {
        "job_title": title_match.group(1).strip() if title_match else "QA Opportunity",
        "company": company_match.group(1).strip() if company_match else "Unknown Client",
        "apply_url": url_match.group(1).strip() if url_match else "",
    }


def evaluate_and_notify_single_job(
    job_description: str,
    job_title: str,
    company: str,
    apply_url: str,
    job_id: str,
    base_cv: str,
    source: str = "",
    location: str = "",
    job_type: str = "",
    is_india: bool = False,
    salary: str = "",
) -> tuple[str, int]:
    """
    Evaluates a single job against base CV, compiles PDF if viable, and sends Telegram alert.
    Returns (outcome, match_score) where outcome is OUTCOME_VIABLE, OUTCOME_REJECTED or OUTCOME_ERROR.
    OUTCOME_ERROR means nothing should be persisted as processed (the job is retried next run).
    """
    india_tag = " [🇮🇳 India/APAC]" if is_india else ""
    print(f"\n[*] Evaluando: '{job_title}' en '{company}' ({source}){india_tag}...")

    quick_reason = quick_reject_reason(salary=salary, description=job_description, is_india=is_india)
    if quick_reason:
        print(f"    [DESCARTE-PREFILTRO] {quick_reason}")
        return OUTCOME_REJECTED, 0

    try:
        evaluation = gemini_evaluator.evaluate_and_tailor_job(
            job_description=job_description,
            base_cv=base_cv,
        )
    except gemini_evaluator.EvaluationError as exc:
        print(f"    [ERROR] Evaluacion fallida, se reintentara en la proxima pasada: {exc}", file=sys.stderr)
        return OUTCOME_ERROR, 0

    print(f"    - Viable: {evaluation.is_viable} | Fit Score: {evaluation.match_score}/100")
    print(f"    - Tarifa: {evaluation.rate_evaluation}")
    print(f"    - Carga:  {evaluation.workload_evaluation}")

    if not evaluation.is_viable:
        reason = evaluation.match_reason or "No cumple con las restricciones de carga o tarifa."
        print(f"    [DESCARTE] {reason}")
        return OUTCOME_REJECTED, evaluation.match_score

    print(f"    [APROBADO] ¡Oferta viable! Preparando CV adaptado y alerta...")

    # Compilar tailored CV a PDF
    output_dir = Path("output").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", job_id)[:30]
    pdf_path = output_dir / f"tailored_cv_{clean_id}.pdf"

    cv_markdown = evaluation.tailored_cv_markdown
    if not cv_markdown.strip():
        print("    [AVISO] Gemini no devolvio CV adaptado; se usa el CV base sin adaptar.", file=sys.stderr)
        cv_markdown = base_cv

    try:
        generate_pdf_from_markdown(cv_markdown, pdf_path)
        print(f"    [OK] PDF generado: {pdf_path.name} ({pdf_path.stat().st_size} bytes)")
    except Exception as exc:
        print(f"    [ERROR] Fallo al generar PDF: {exc}", file=sys.stderr)
        pdf_path = None

    # Enviar alerta a Telegram con metadatos completos
    match_reason = evaluation.match_reason
    if evaluation.ats_keywords_matched:
        match_reason += f"\nATS keywords usadas: {', '.join(evaluation.ats_keywords_matched)}"
    if evaluation.ats_keywords_missing:
        match_reason += f"\nKeywords faltantes en tu CV: {', '.join(evaluation.ats_keywords_missing)}"
    cover_letter = evaluation.cover_letter

    sent = telegram_notifier.send_telegram_alert(
        match_reason=match_reason,
        cover_letter=cover_letter,
        apply_url=apply_url,
        pdf_path=pdf_path,
        job_title=job_title,
        company=company,
        rate_info=evaluation.rate_evaluation,
        workload_info=evaluation.workload_evaluation,
        score=evaluation.match_score,
        source=source,
        location=location,
        job_type=job_type,
        is_india=is_india,
    )

    if not sent:
        print("    [ERROR] No se pudo entregar la notificacion a Telegram; se reintentara en la proxima pasada.", file=sys.stderr)
        return OUTCOME_ERROR, evaluation.match_score

    print("    [OK] ¡Alerta enviada exitosamente a Telegram!")
    return OUTCOME_VIABLE, evaluation.match_score


def run_live_hunt(
    limit_per_source: int = 10,
    sources: Optional[list[str]] = None,
    allow_india: Optional[bool] = None,
    freelance_only: bool = False,
    india_only: bool = False,
) -> int:
    """
    Fetches real QA job opportunities from the web, deduplicates against history,
    evaluates them with Gemini, and dispatches viable ones to Telegram.
    Returns count of viable jobs processed.
    """
    print("\n" + "=" * 70)
    print(">> INICIANDO BUSQUEDA EN VIVO DE OFERTAS LABORALES REALES (WEB)")
    print("=" * 70)

    try:
        base_cv = load_base_cv()
        print(f"[*] CV Base cargado ({len(base_cv)} caracteres).")
    except Exception as exc:
        print(f"[FATAL] Error cargando CV base: {exc}", file=sys.stderr)
        return 0

    history_mgr = JobHistoryManager()
    new_jobs = fetch_all_new_qa_jobs(
        history_manager=history_mgr,
        limit_per_source=limit_per_source,
        sources=sources,
        allow_india=allow_india,
        freelance_only=freelance_only,
        india_only=india_only,
    )

    if not new_jobs:
        print("[*] No se encontraron ofertas nuevas sin procesar en esta pasada.")
        return 0

    viable_count = 0

    for idx, job in enumerate(new_jobs, start=1):
        print(f"\n--- [OFERTA {idx}/{len(new_jobs)}] ---")
        job_text = job.to_evaluation_text()

        try:
            outcome, score = evaluate_and_notify_single_job(
                job_description=job_text,
                job_title=job.title,
                company=job.company,
                apply_url=job.url,
                job_id=job.id,
                base_cv=base_cv,
                source=job.source,
                location=job.location,
                job_type=job.job_type,
                is_india=job.is_india,
                salary=job.salary,
            )

            # Only persist jobs that were truly evaluated (and notified, if viable).
            # Errors (Gemini/Telegram) are retried on the next pass.
            if outcome != OUTCOME_ERROR:
                history_mgr.mark_processed(
                    job=job,
                    is_viable=(outcome == OUTCOME_VIABLE),
                    match_score=score,
                    notes="Procesado en búsqueda en vivo",
                )

            if outcome == OUTCOME_VIABLE:
                viable_count += 1

            # Pausa breve entre llamadas a la API de Gemini para evitar rate limits
            time.sleep(1.5)

        except Exception as exc:
            print(f"[ERROR] Error procesando oferta '{job.title}': {exc}", file=sys.stderr)

    print("\n" + "=" * 70)
    print(f">> Búsqueda completada: {viable_count} oferta(s) viable(s) enviada(s) a Telegram.")
    print("=" * 70)
    return viable_count



def run_pipeline_sample() -> None:
    """Runs pipeline on a sample hardcoded job posting for testing purposes."""
    sample_job = """
Job Title: Freelance QA Specialist (Playwright & Manual Testing)
Company: SaaS ScaleUp Studio
Location: Remote (Worldwide / Flexible Schedule)
Type: Freelance / Part-time Contract
Workload: 5 to 8 hours per week (100% asynchronous, no fixed meetings)
Rate / Budget: $25.00 - $30.00 USD / hr
Apply URL: https://www.upwork.com/freelance-jobs/playwright-qa-specialist-45678

About the Project:
We are an agile product studio maintaining a B2B SaaS web app. We are seeking an independent,
reliable QA Engineer for 5-8 hours per week to conduct thorough exploratory manual testing
and maintain our automated end-to-end regression suites.

Responsibilities:
- Perform exploratory testing on weekly staging builds and new user flows.
- Build and maintain automated regression tests using Playwright (Python or TypeScript).
- Verify backend endpoints and webhook responses using Postman.
- Document clear bug reports in Jira with reproducible steps, screenshots, and Loom video clips.
- Communicate asynchronously via Slack and GitHub with minimal need for live calls.

Requirements:
- Proven practical experience with manual testing and Playwright automation.
- Ability to work autonomously in an asynchronous environment.
- Strong attention to detail and clear bug documentation.
"""
    base_cv = load_base_cv()
    meta = extract_job_metadata(sample_job)
    evaluate_and_notify_single_job(
        job_description=sample_job,
        job_title=meta["job_title"],
        company=meta["company"],
        apply_url=meta["apply_url"],
        job_id="sample_test_001",
        base_cv=base_cv,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="qaJobHunterBot - Freelance QA Job Hunter & Tailored CV Generator")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Ejecutar prueba con oferta simulada en lugar de buscar en la web.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Límite máximo de ofertas a inspeccionar por cada fuente (default: 10).",
    )
    parser.add_argument(
        "--freelance-only",
        action="store_true",
        help="Filtrar únicamente proyectos freelance y pagos por proyecto (sin relación de dependencia).",
    )
    parser.add_argument(
        "--india-only",
        action="store_true",
        help="Filtrar únicamente ofertas y proyectos de la zona de India / APAC.",
    )
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="MINUTES",
        help="Ejecutar en bucle continuo cada N minutos (ej: --loop 60).",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="",
        help="Fuentes específicas separadas por coma (ej: freelancer,remotive,jobicy,remoteok,hasjob).",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Mostrar estadisticas del historial (SQLite) y salir.",
    )
    parser.add_argument(
        "--reevaluate-legacy",
        action="store_true",
        help="Olvidar los registros migrados del JSON antiguo para volver a evaluarlos con el pipeline actual.",
    )

    args = parser.parse_args()

    if args.stats or args.reevaluate_legacy:
        with JobHistoryManager() as history:
            if args.reevaluate_legacy:
                removed = history.forget_legacy()
                print(f"[*] {removed} registros legacy eliminados; se volveran a evaluar en la proxima busqueda.")
            print(f"[*] Historial: {history.stats()}")
        sys.exit(0)


    selected_sources = [s.strip() for s in args.sources.split(",") if s.strip()] or None

    if args.test:
        print("[*] Modo de prueba seleccionado.")
        run_pipeline_sample()
    elif args.loop > 0:
        print(f"[*] Modo continuo activado. Búsqueda cada {args.loop} minutos. Presiona Ctrl+C para detener.")
        try:
            while True:
                run_live_hunt(
                    limit_per_source=args.limit,
                    sources=selected_sources,
                    freelance_only=args.freelance_only,
                    india_only=args.india_only,
                )
                print(f"\n[*] Esperando {args.loop} minutos hasta la próxima búsqueda...")
                time.sleep(args.loop * 60)
        except KeyboardInterrupt:
            print("\n[*] Bot detenido por el usuario.")
    else:
        # Ejecución estándar: búsqueda en vivo
        run_live_hunt(
            limit_per_source=args.limit,
            sources=selected_sources,
            freelance_only=args.freelance_only,
            india_only=args.india_only,
        )

