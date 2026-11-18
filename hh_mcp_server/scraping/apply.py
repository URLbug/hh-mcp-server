import logging
import re

from playwright.async_api import Page

from hh_mcp_server.constants import BASE_URL
from hh_mcp_server.scraping import selectors as S
from hh_mcp_server.scraping.extractor import navigate_and_wait
from hh_mcp_server.exceptions import ApplyError

logger = logging.getLogger(__name__)


async def detect_questions(page: Page) -> list[dict]:
    """Detect employer questions in the apply form.

    Works for both popup and full-page response forms.
    Questions are textareas with name='task_*_text'.
    """
    questions = []

    result = await page.evaluate("""() => {
        const textareas = document.querySelectorAll('textarea[name^="task_"]');
        const out = [];
        for (const ta of textareas) {
            const name = ta.getAttribute('name') || '';
            // Walk up to find the question container and extract label
            let el = ta;
            let labelText = '';
            for (let i = 0; i < 8 && el.parentElement; i++) {
                el = el.parentElement;
                // Look for text nodes that are the question label
                const children = el.children;
                for (const child of children) {
                    // Skip the textarea wrapper itself
                    if (child.querySelector('textarea')) continue;
                    const t = child.textContent.trim();
                    if (t && t.length > 3 && t.length < 500
                        && !t.includes('Писать тут')
                        && !t.includes('0/')
                        && t !== 'Ответьте на вопросы') {
                        labelText = t;
                        break;
                    }
                }
                if (labelText) break;
            }
            out.push({name: name, label: labelText});
        }
        return out;
    }""")

    for item in result:
        questions.append({
            "label": item["label"],
            "type": "textarea",
            "name": item["name"],
        })

    return questions


async def fill_questions(page: Page, questions: list[dict], answers: dict[str, str]) -> None:
    """Fill in question answers by matching label text or name to textarea."""
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    normalized_answers = {
        normalize(key): value for key, value in answers.items() if isinstance(value, str)
    }
    missing: list[str] = []

    for q in questions:
        label = q["label"]
        name = q["name"]
        # Accept either the generated textarea name or the visible question.
        answer = answers.get(name) or answers.get(label)
        if answer is None:
            answer = normalized_answers.get(normalize(name))
        if answer is None:
            answer = normalized_answers.get(normalize(label))
        if answer is None:
            # Some callers omit punctuation or use a shortened question.
            label_key = normalize(label)
            for key, value in normalized_answers.items():
                if label_key and (label_key in key or key in label_key):
                    answer = value
                    break
        if answer is None or not isinstance(answer, str):
            logger.warning("No answer found for question: %s (name=%s)", label, name)
            missing.append(name)
            continue

        textarea = page.locator(f'textarea[name="{name}"]').first
        if not await textarea.count():
            missing.append(name)
            continue
        await textarea.fill(answer)
        logger.info("Filled question '%s' with answer", label[:50])

        if not (await textarea.input_value()).strip():
            missing.append(name)

    if missing:
        raise ApplyError(f"Required questions were not answered: {', '.join(missing)}")


async def dismiss_cookie_banner(page: Page) -> None:
    """Dismiss cookie consent banner if present."""
    try:
        btn = await page.query_selector("[data-qa='cookies-policy-informer-accept']")
        if btn:
            await btn.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass


async def _fill_cover_letter(page: Page, cover_letter: str) -> None:
    """Fill the current hh.ru cover-letter textarea and verify its value."""
    if not cover_letter.strip():
        raise ApplyError("Cover letter must not be empty.")

    # Older response forms hid the textarea behind a toggle. The current
    # Magritte modal renders it directly, but keeping the toggle fallback
    # makes the flow work with both variants.
    letter_input = page.locator(S.COVER_LETTER_INPUT).first
    try:
        await letter_input.wait_for(state="visible", timeout=5000)
    except Exception:
        # In the current Magritte modal the letter editor is not mounted until
        # the user clicks "Добавить сопроводительное".
        toggle = page.locator(S.COVER_LETTER_TOGGLE).first
        if await toggle.count():
            await toggle.click()
        try:
            await letter_input.wait_for(state="visible", timeout=10000)
        except Exception as exc:
            raise ApplyError(
                "Cover letter field did not appear in the response form "
                "(expected data-qa='vacancy-response-popup-form-letter-input')."
            ) from exc

    await letter_input.fill(cover_letter)
    actual_value = await letter_input.input_value()
    if actual_value != cover_letter:
        # React-controlled textareas can occasionally overwrite a synthetic
        # input event; retry via keyboard input and verify once more.
        await letter_input.click()
        await letter_input.press("Control+A")
        await letter_input.type(cover_letter)
        actual_value = await letter_input.input_value()
    if actual_value != cover_letter:
        raise ApplyError("Cover letter could not be filled into the response form.")
    logger.info("Cover letter filled (%d characters)", len(cover_letter))


async def apply_to_vacancy(
    page: Page,
    vacancy_id: str,
    resume_id: str | None = None,
    cover_letter: str | None = None,
    question_answers: dict[str, str] | None = None,
) -> dict:
    url = f"{BASE_URL}/vacancy/{vacancy_id}"
    await navigate_and_wait(page, url)

    await dismiss_cookie_banner(page)

    # Check if already applied
    already = await page.query_selector(S.ALREADY_APPLIED)
    if already:
        return {"status": "already_applied", "message": "You have already applied to this vacancy."}

    # Click apply button
    apply_btn = await page.query_selector(S.APPLY_BUTTON)
    if not apply_btn:
        return {"status": "error", "message": "Apply button not found. Vacancy may be closed."}

    await apply_btn.click()
    await page.wait_for_timeout(2000)

    # Handle relocation warning popup if it appears
    relocation_confirm = await page.query_selector("[data-qa='relocation-warning-confirm']")
    if relocation_confirm:
        await relocation_confirm.click()
        await page.wait_for_timeout(2000)

    # Check if redirected to full-page response form
    is_response_page = "vacancy_response" in page.url

    # New hh.ru modals render asynchronously and use form[name="vacancy_response"]
    # without the legacy data-qa wrapper. Wait for the actual form instead of
    # taking a single DOM snapshot after the click.
    form = None
    try:
        await page.wait_for_selector(S.APPLY_FORM, state="attached", timeout=10000)
        form = await page.query_selector(S.APPLY_FORM)
    except Exception:
        form = None

    if not form and not is_response_page:
        # Maybe it applied directly (one-click apply)
        if "negotiations" in page.url:
            return {"status": "success", "message": "Applied successfully (direct apply)."}

        # Re-check if already applied (button click may have triggered a quick-apply)
        already_after_click = await page.query_selector(S.ALREADY_APPLIED)
        if already_after_click:
            return {"status": "already_applied", "message": "You have already applied to this vacancy."}

        return {"status": "error", "message": "Apply form did not appear."}

    # The modal can be present even when the legacy wrapper is absent. Use the
    # actual form as the scope for all subsequent controls when possible.
    if form:
        logger.info("Response form detected: %s", await form.get_attribute("id"))

    # Detect questions
    questions = await detect_questions(page)
    if questions and not question_answers:
        return {
            "status": "questions_required",
            "message": "This vacancy has questions that need to be answered. Call again with question_answers.",
            "questions": questions,
        }

    # Fill questions if provided
    if question_answers and questions:
        try:
            await fill_questions(page, questions, question_answers)
        except ApplyError as exc:
            return {"status": "error", "message": str(exc), "questions": questions}

    # Fill cover letter
    if cover_letter:
        try:
            await _fill_cover_letter(page, cover_letter)
        except ApplyError as exc:
            return {"status": "error", "message": str(exc)}

    # Submit
    submit_btn = await page.query_selector(S.SUBMIT_BUTTON)
    if not submit_btn:
        return {"status": "error", "message": "Submit button not found."}

    # The current button submits RESPONSE_MODAL_FORM_ID. Check the field one
    # last time so a submit can never silently discard the cover letter.
    if cover_letter:
        letter_input = page.locator(S.COVER_LETTER_INPUT).first
        if not await letter_input.count() or (await letter_input.input_value()) != cover_letter:
            return {"status": "error", "message": "Cover letter is not present in the form."}

    await submit_btn.click()
    await page.wait_for_timeout(3000)

    # Check if still on response page (submit failed due to validation)
    if "vacancy_response" in page.url:
        # Check for validation errors
        error_text = await page.evaluate("""() => {
            const el = document.querySelector('[data-qa="vacancy-response-popup-error"]');
            if (el) return el.textContent.trim();
            // A question textarea remains in the DOM after filling. Only an
            // empty value means validation failed because it was unanswered.
            const q = [...document.querySelectorAll('textarea[name^="task_"]')]
                .find((textarea) => !textarea.value.trim());
            if (q) return 'Required questions were not answered.';
            return '';
        }""")
        if error_text:
            return {"status": "error", "message": error_text}

    # Check for error in popup form
    error_el = await page.query_selector("[data-qa='vacancy-response-popup-error']")
    if error_el:
        error_text = await error_el.inner_text()
        return {"status": "error", "message": error_text.strip()}

    return {"status": "success", "message": f"Applied to vacancy {vacancy_id} successfully."}
