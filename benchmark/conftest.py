def pytest_addoption(parser):
    parser.addoption(
        "--mode",
        action="store",
        default="xpu",
        required=False,
        choices=["xpu", "cpu"],
        help="record latency in xpu or cpu",
    )


def pytest_configure(config):
    value = config.getoption("--mode")
    global CPU_MODE
    CPU_MODE = value == "cpu"
