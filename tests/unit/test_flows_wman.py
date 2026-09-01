def test_wman_round_trip():
    original = Wman(dt.date(2026, 9, 1), 37, (ActiveUnit("2__ABCDE0001"),))
    payload = file.build(spec.SPEC[wman.FILE_TYPE], header, wman.to_nodes(original))
    _, body = file.parse(payload, spec.SPEC[wman.FILE_TYPE])
    assert wman.from_nodes(body) == original