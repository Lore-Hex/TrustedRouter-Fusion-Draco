

def test_the_judge_is_addressed_through_the_gateway() -> None:
    """inspect_ai has its own google provider: a bare 'google/...' judge id resolves to
    the Google API and bypasses TrustedRouter (and AnyEval's billing and pinning)."""
    from draco.task import DEFAULT_JUDGE_MODEL

    assert DEFAULT_JUDGE_MODEL.startswith("trustedrouter/"), DEFAULT_JUDGE_MODEL
    assert DEFAULT_JUDGE_MODEL.endswith("google/gemini-3.1-pro-preview")
