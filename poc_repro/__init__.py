from .pipeline import PipelineResult, run_pipeline


def run_sample(**kwargs) -> PipelineResult:
    """Run the bundled sample testcase with one function call."""
    defaults = {
        "testcase": "sample",
        "clean": True,
    }
    defaults.update(kwargs)
    return run_pipeline(**defaults)


__all__ = ["PipelineResult", "run_pipeline", "run_sample"]
