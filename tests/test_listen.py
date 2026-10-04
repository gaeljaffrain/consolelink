"""--listen and --allow-write [PASSWORD]: which combinations are accepted."""
import pytest

from consolelink import app


def parse(*argv):
    parser = app.build_parser()
    args = parser.parse_args(list(argv))
    app.check_args(parser, args)
    return args


def test_default_is_localhost():
    args = parse()
    assert args.listen == "localhost"
    assert app.LISTEN_HOSTS[args.listen] == "127.0.0.1"
    assert app.LISTEN_HOSTS["network"] == "0.0.0.0"


def test_allow_write_password_is_optional_on_localhost():
    assert parse("--allow-write").allow_write == ""
    assert parse("--allow-write", "pw").allow_write == "pw"
    assert parse("--listen", "localhost", "--allow-write").allow_write == ""


def test_network_write_needs_a_password():
    assert parse("--listen", "network", "--allow-write", "pw").allow_write == "pw"
    assert parse("--listen", "network").allow_write is None  # read-only, no password involved
    with pytest.raises(SystemExit):
        parse("--listen", "network", "--allow-write")


def test_listen_and_allow_write_need_the_web_page():
    with pytest.raises(SystemExit):
        parse("--no-web", "--debug", "--listen", "network")
    with pytest.raises(SystemExit):
        parse("--no-web", "--debug", "--allow-write")
