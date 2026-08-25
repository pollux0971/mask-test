import pytest

from host.control.commands import amb_command, mel_command, sens_command


def test_sens_command_on():
    assert sens_command("A", True) == "SENS:A=1"


def test_sens_command_off():
    assert sens_command("B", False) == "SENS:B=0"


def test_sens_command_rejects_invalid_sensor():
    with pytest.raises(ValueError):
        sens_command("C", True)


def test_mel_command_on_off():
    assert mel_command(True) == "MEL:1"
    assert mel_command(False) == "MEL:0"


def test_amb_command_on_off():
    assert amb_command(True) == "AMB:1"
    assert amb_command(False) == "AMB:0"
