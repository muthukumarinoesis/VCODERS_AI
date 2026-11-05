import io
import time

import numpy as np
import pytest
from fake_llm import FakeLLM
from fastapi.testclient import TestClient
from fixtures import ENGLISH, image_bytes, page_picture, scanned_pdf, text_pdf
from PIL import Image, ImageDraw, ImageFilter
from svc import ThreadPool, make_ctx
from test_runner import config, job

from examples.fastapi_app import create_app
from pipeline_router import FileRefused, Prober, SafetyCaps, sniff
from pipeline_router.quality import PageQuality, file_quality, garbled_share, image_quality
from quiz_runner import QuizRunner, RunnerError, read

A4_WIDTH_IN = 8.27


def clean(dpi=200):
    return page_picture(round(A4_WIDTH_IN * dpi))


def blurred(px=4):
    return clean().filter(ImageFilter.GaussianBlur(px))


def dark():
    return clean().point(lambda v: v * 0.35)


def faint():
    return clean().point(lambda v: 150 + v * 0.25)


def noisy():
    a = np.asarray(clean().convert("L"), np.float32)
    return Image.fromarray(
        np.clip(a + np.random.default_rng(0).normal(0, 30, a.shape), 0, 255).astype(np.uint8)
    )


def q(img, dpi=None):
    return image_quality(img, A4_WIDTH_IN if dpi is None else img.width / dpi)


# ---- the scorer -------------------------------------------------------------------


def test_clean_pages_score_high():
    assert q(clean()).score >= 90 and q(clean()).issues == ()
    sparse = Image.new("RGB", clean().size, "white")
    from PIL import ImageFont

    ImageDraw.Draw(sparse).text((200, 200), "Chapter one", fill="black", font=ImageFont.load_default(size=40))
    assert q(sparse).score >= 90  # a page with one heading is fine, not "blurry"


@pytest.mark.parametrize("make,issue,band", [
    (lambda: blurred(2), "blurry", "warn"),
    (lambda: blurred(4), "blurry", "reject"),
    (dark, "dark", "reject"),
    (faint, "low_contrast", "warn-or-ok"),
    (noisy, "noisy", "ok"),
    (lambda: clean().rotate(5, fillcolor="white"), "skewed", "ok"),
    (lambda: clean(72), "low_resolution", "warn"),
    (lambda: blurred(3).point(lambda v: v * 0.5), "dark", "reject"),
])  # fmt: skip
def test_each_problem_is_found_and_weighted(make, issue, band):
    result = q(make())
    assert issue in result.issues
    if band == "reject":
        assert result.score < 40
    elif band == "warn":
        assert 40 <= result.score < 70
    elif band == "warn-or-ok":
        assert result.score >= 40
    else:
        assert result.score >= 70


def test_glare_on_a_photographed_page():
    photo = clean().point(lambda v: v * 0.78)
    ImageDraw.Draw(photo).ellipse((300, 400, 1300, 1400), fill="white")
    assert "glare" in q(photo).issues


def test_cut_off_text():
    page = clean()
    assert "cut_off" in q(page.crop((page.width // 4, 0, page.width, page.height))).issues


def test_blank_page_is_left_out():
    blank = q(Image.new("RGB", clean().size, "white"))
    assert blank.score is None and blank.issues == ("blank",)
    assert file_quality([blank, PageQuality(90)]) == (90, ())
    assert file_quality([blank]) == (0, ("blank",))
    assert file_quality([]) == (None, ())


def test_file_score_is_the_mean_and_issues_come_from_weak_pages():
    pages = [PageQuality(90, ("noisy",)), PageQuality(30, ("blurry",)), PageQuality(60, ("dark",))]
    assert file_quality(pages) == (60, ("blurry", "dark"))


def test_scoring_is_fast_enough():
    start = time.perf_counter()
    q(clean(300))
    assert time.perf_counter() - start < 3  # about 0.2 s on a quiet machine


def test_garbled_text():
    assert garbled_share("(cid:12)(cid:13)(cid:14) (cid:15)") > 0.9
    assert garbled_share("The cafÃ© is Ã©lÃ©gant â€™s") > 0.3
    assert garbled_share("�� text") > 0.3
    assert garbled_share(ENGLISH) == 0 and garbled_share("தமிழ் ஒரு மொழி") == 0 and garbled_share("") == 0


# ---- inspection -------------------------------------------------------------------------


def inspect(data, **kw):
    caps = SafetyCaps()
    return Prober(caps, **kw).probe(data, sniff(data, caps))


def test_blurred_scan_is_refused_with_the_reason():
    with pytest.raises(FileRefused) as e:
        inspect(scanned_pdf(3, picture=blurred(4)))
    assert e.value.code == "poor_quality"
    assert e.value.message.startswith("The uploaded file is not good enough quality (it is blurry)")


def test_dark_photo_is_refused():
    with pytest.raises(FileRefused) as e:
        inspect(image_bytes("JPEG", dark().convert("RGB")))
    assert e.value.code == "poor_quality" and "too dark" in e.value.message


def test_weak_scan_is_accepted_with_a_warning():
    facts = inspect(scanned_pdf(3, picture=blurred(2)))
    assert 40 <= facts.quality_score < 70 and facts.quality_issues == ("blurry",)


def test_good_scan_and_text_pdf():
    assert inspect(scanned_pdf(3)).quality_score >= 90
    text = inspect(text_pdf(3))
    assert (text.quality_score, text.quality_issues) == (None, ())  # text layers have no picture to score


def test_quality_check_can_be_turned_off():
    assert inspect(scanned_pdf(3, picture=blurred(4)), quality=None).quality_score is None


def test_garbled_text_layer_is_read_as_a_scan():
    garbage = "(cid:3)(cid:17)(cid:22)(cid:5)(cid:9) " * 20
    facts = inspect(text_pdf(4, body=garbage))
    assert facts.scanned and "garbled" in facts.quality_issues
    pages = read("pdf", text_pdf(2, body=garbage), {"pdf": {"impl": "pymupdf"}})
    assert all(p.needs_ocr and p.text == "" for p in pages)


def test_quality_goes_into_the_decision_and_the_api(tmp_path):
    ctx = make_ctx(tmp_path)
    client = TestClient(
        create_app(ctx, pool=ThreadPool(), inspect=lambda d, mb, h: ctx.routing.inspect(d, mb, h))
    )
    h = {"X-API-Key": ctx.key}
    r = client.post("/v1/route", headers=h, files={"file": ("s.pdf", scanned_pdf(2, picture=blurred(2)))})
    decision = r.json()["decision"]
    assert (
        r.status_code == 200
        and 40 <= decision["quality_score"] < 70
        and decision["quality_issues"] == ["blurry"]
    )
    r = client.post("/v1/route", headers=h, files={"file": ("s.pdf", scanned_pdf(2, picture=blurred(4)))})
    assert (r.status_code, r.json()["error"]) == (400, "poor_quality")


# ---- the worker ------------------------------------------------------------------------


def test_worker_skips_pages_too_poor_to_read():
    pictures = [page_picture(seed=1), blurred(5), page_picture(seed=2), dark()]
    llm = FakeLLM(transcript=ENGLISH * 5)
    result = QuizRunner(llm)(config(3), job(), scanned_pdf(4, picture=pictures))
    assert result["skipped_pages"] == [2, 4] and len(llm.image_calls) == 2 and result["made"] == 3


def test_worker_refuses_when_every_page_is_too_poor():
    with pytest.raises(RunnerError) as e:
        QuizRunner(FakeLLM(transcript=ENGLISH * 5))(config(3), job(), scanned_pdf(2, picture=blurred(5)))
    assert e.value.code == "poor_quality" and "blurry" in e.value.message


def test_facts_with_quality_round_trip():
    from pipeline_router import FileFacts

    facts = inspect(scanned_pdf(2, picture=blurred(2)))
    assert FileFacts.from_dict(facts.to_dict()) == facts
    old = {k: v for k, v in facts.to_dict().items() if not k.startswith("quality")}
    assert FileFacts.from_dict(old).quality_score is None  # facts stored before this change still load


def test_image_bytes_helper_is_unchanged():
    assert Image.open(io.BytesIO(image_bytes("PNG"))).size == (20, 10)
