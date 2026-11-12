import numpy as np
import pytest

from meetaras.align import SAMPLE_RATE, AlignmentError, align, align_files
from tests.conftest import EXT_DRIFT_PPM, EXT_OFFSET, bursts, external_from

SR = SAMPLE_RATE


@pytest.mark.parametrize("offset,drift", [(3.2, 0.0), (-1.7, 0.0), (12.0, 80.0), (0.4, -60.0)])
def test_recovers_offset_and_drift(offset, drift):
    x = bursts(240, seed=3, sr=SR)  # 4 minutes
    ext = external_from(x, offset, drift, sr=SR)
    a = align(x, ext, SR)
    assert a.offset == pytest.approx(offset, abs=0.002)
    assert a.drift_ppm == pytest.approx(drift, abs=10)


def test_drift_is_what_matters_at_the_end():
    # 45-minute talk with 40 ppm drift: ~108 ms off at the end if uncorrected.
    x = bursts(45 * 60, seed=5, sr=SR)
    ext = external_from(x, 5.0, 40.0, sr=SR)
    a = align(x, ext, SR)
    end = 45 * 60
    assert abs(a.ext_time(end) - (5.0 + end * (1 + 40e-6))) < 0.005


def test_unrelated_audio_fails():
    with pytest.raises(AlignmentError, match="could not find"):
        align(bursts(60, seed=1, sr=SR), bursts(80, seed=2, sr=SR), SR)


def test_align_files(fixtures, tmp_path):
    a = align_files(fixtures / "talk.mp4", fixtures / "mic.wav", tmp_path)
    assert a.offset == pytest.approx(EXT_OFFSET, abs=0.003)
    # 12 s clip: windows only ~8 s apart, so 0.5 ms of timing noise is ~60 ppm. Long-talk precision is
    # covered by the numpy tests above.
    assert a.drift_ppm == pytest.approx(EXT_DRIFT_PPM, abs=150)
    assert list(tmp_path.glob("align-*.json"))  # cached
    assert align_files(fixtures / "talk.mp4", fixtures / "mic.wav", tmp_path).offset == a.offset


def test_external_from_model():
    x = np.zeros(SR * 2, dtype=np.float32)
    x[SR] = 1.0  # click at t=1s
    ext = external_from(x, 0.5, 0.0, sr=SR)
    assert abs(np.argmax(np.abs(ext)) / SR - 1.5) < 0.001
