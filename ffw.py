#!/usr/bin/env python3
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from PyQt5 import QtCore, QtWidgets, uic
# $ sudo python3 ffw.py

UI_DEFAULT = Path(__file__).with_name("FFW.ui")


def run_command(cmd):
    """Run a command and return (ok, stdout+stderr)."""
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()
    except Exception as exc:
        return False, str(exc)


def is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def interface_is_virtual(ifname: str) -> bool:
	#
    banned_prefixes = (
        "lo", "docker", "enx","virbr", "veth", "tun", "tap", "wg", "zt",
        "vmnet", "vboxnet",
    )
    if ifname.startswith(banned_prefixes):
        return True

    sys_path = Path("/sys/class/net") / ifname
    try:
        resolved = sys_path.resolve()
        if "/devices/virtual/" in str(resolved):
            return True
    except Exception:
        pass
    return False


def interface_is_ethernet(ifname: str) -> bool:
    type_path = Path("/sys/class/net") / ifname / "type"
    try:
        return type_path.read_text().strip() == "1"
    except Exception:
        return False


def list_physical_ethernet_interfaces():
    base = Path("/sys/class/net")
    results = []
    if not base.exists():
        return results

    for entry in sorted(base.iterdir()):
        ifname = entry.name
        if is_virtual_name_only(ifname):
            continue
        if interface_is_virtual(ifname):
            continue
        if not interface_is_ethernet(ifname):
            continue
        results.append(ifname)
    return results


def is_virtual_name_only(ifname: str) -> bool:
    return ifname == "lo"


def read_text(path: Path):
    try:
        return path.read_text().strip()
    except Exception:
        return ""


def has_carrier(ifname: str) -> bool:
    return read_text(Path("/sys/class/net") / ifname / "carrier") == "1"


def is_up(ifname: str) -> bool:
    return read_text(Path("/sys/class/net") / ifname / "operstate") == "up"


def negotiated_speed_mbps(ifname: str):
    speed_path = Path("/sys/class/net") / ifname / "speed"
    try:
        value = speed_path.read_text().strip()
        if value and value not in {"-1", "unknown"}:
            mbps = int(value)
            if mbps > 0:
                return mbps
    except Exception:
        pass

    ok, out = run_command(["ethtool", ifname])
    if not ok and not out:
        return None
    m = re.search(r"Speed:\s*(\d+)\s*Mb/s", out)
    if m:
        return int(m.group(1))
    return None


def supported_speed_guess_mbps(ifname: str):
    ok, out = run_command(["ethtool", ifname])
    if not out:
        return None
    speeds = []
    for match in re.finditer(r"(\d+)base", out):
        try:
            speeds.append(int(match.group(1)))
        except ValueError:
            pass
    return max(speeds) if speeds else None


class TrafficShaper:
    def __init__(self, log_func):
        self.log = log_func

    def build_commands(self, iface: str, base_mbps: int, percent: int, port: int = 502):
        if base_mbps <= 0:
            raise ValueError("Base rate must be greater than zero.")

        rate_mbps = max(1, int(round(base_mbps * (percent / 100.0))))
        ceil_mbps = max(rate_mbps, base_mbps)

        mark = 10
        classid = "1:10"

        cmds = [
            ["tc", "qdisc", "replace", "dev", iface, "root", "handle", "1:", "htb", "default", "999"],
            ["tc", "class", "replace", "dev", iface, "parent", "1:", "classid", "1:1", "htb",
             "rate", f"{base_mbps}mbit", "ceil", f"{base_mbps}mbit"],
            ["tc", "class", "replace", "dev", iface, "parent", "1:1", "classid", classid, "htb",
             "rate", f"{rate_mbps}mbit", "ceil", f"{ceil_mbps}mbit"],
            ["iptables", "-t", "mangle", "-D", "OUTPUT", "-o", iface, "-p", "tcp", "--dport", str(port),
             "-j", "MARK", "--set-mark", str(mark)],
            ["iptables", "-t", "mangle", "-A", "OUTPUT", "-o", iface, "-p", "tcp", "--dport", str(port),
             "-j", "MARK", "--set-mark", str(mark)],
            ["tc", "filter", "replace", "dev", iface, "parent", "1:", "protocol", "ip", "prio", "1",
             "handle", str(mark), "fw", "classid", classid],
        ]
        return rate_mbps, cmds

    def apply(self, iface: str, base_mbps: int, percent: int, port: int = 502):
        rate_mbps, cmds = self.build_commands(iface, base_mbps, percent, port)
        dry_run = not is_root()

        self.log(f"Interface: {iface}")
        self.log(f"Port: {port}")
        self.log(f"Base rate: {base_mbps} Mbps")
        self.log(f"Slider: {percent}%")
        self.log(f"Applied rate: {rate_mbps} Mbps")
        self.log("")

        if dry_run:
            self.log("Not running as root: showing commands only.")
            for cmd in cmds:
                self.log("$ " + shlex.join(cmd))
            return rate_mbps, False

        for cmd in cmds:
            self.log("$ " + shlex.join(cmd))
            ok, out = run_command(cmd)
            if out:
                self.log(out)
            if not ok:
                raise RuntimeError(f"Command failed: {shlex.join(cmd)}")
        return rate_mbps, True


class MainDialog(QtWidgets.QDialog):
    def __init__(self, ui_path: Path):
        super().__init__()
        uic.loadUi(str(ui_path), self)

        self.shaper = TrafficShaper(self.append_status)
        self.resize(900, 600)
        self.setWindowTitle("FFW - Port 502 Rate Limit")

        self._rebuild_layout()
        self._configure_widgets()
        self._load_interfaces()
        self._connect_signals()
        self._update_display()

    def _rebuild_layout(self):
        existing = self.layout()
        if existing is not None:
            QtWidgets.QWidget().setLayout(existing)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # New controls 
        iface_row = QtWidgets.QHBoxLayout()
        self.ifaceLabel = QtWidgets.QLabel("Interface:")
        self.ifaceCombo = QtWidgets.QComboBox()
        self.refreshButton = QtWidgets.QPushButton("Refresh")
        iface_row.addWidget(self.ifaceLabel)
        iface_row.addWidget(self.ifaceCombo, 1)
        iface_row.addWidget(self.refreshButton)

        speed_row = QtWidgets.QHBoxLayout()
        self.detectedSpeedLabel = QtWidgets.QLabel("Detected: unknown")
        self.linkStateLabel = QtWidgets.QLabel("Link: unknown")
        self.baseRateLabel = QtWidgets.QLabel("Base rate (Mbps):")
        self.baseRateSpin = QtWidgets.QSpinBox()
        self.baseRateSpin.setRange(1, 100000)
        self.baseRateSpin.setValue(1000)
        speed_row.addWidget(self.detectedSpeedLabel)
        speed_row.addSpacing(20)
        speed_row.addWidget(self.linkStateLabel)
        speed_row.addStretch(1)
        speed_row.addWidget(self.baseRateLabel)
        speed_row.addWidget(self.baseRateSpin)

        # widgets from FFW.ui
        port_row = QtWidgets.QHBoxLayout()
        port_row.addWidget(self.label_3)
        port_row.addWidget(self.lineEdit)
        port_row.addStretch(1)
        self.currentRateLabel = QtWidgets.QLabel("")
        port_row.addWidget(self.currentRateLabel)

        root.addLayout(iface_row)
        root.addLayout(speed_row)
        root.addWidget(self.label)
        root.addWidget(self.horizontalSlider)
        lbl_row = QtWidgets.QHBoxLayout()
        lbl_row.addWidget(self.label)
        lbl_row.addStretch(1)
        lbl_row.addWidget(self.label_2)
        root.addLayout(port_row)
        root.addWidget(self.horizontalSlider)
        root.addLayout(lbl_row)
        root.addWidget(self.pushButton)
        root.addWidget(self.label_4)
        root.addWidget(self.textBrowser, 1)

    def _configure_widgets(self):
        self.lineEdit.setText("502")
        self.lineEdit.setReadOnly(True)
        self.lineEdit.setToolTip("Fixed for now: Modbus/TCP port 502")
        self.pushButton.setText("Apply port 502 limit")
        self.label.setText("Min: 1%")
        self.label_2.setText("Max: 100%")
        self.label_4.setText("Status:")
        self.horizontalSlider.setRange(1, 100)
        self.horizontalSlider.setValue(100)
        self.horizontalSlider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.horizontalSlider.setTickInterval(10)
        self.textBrowser.setOpenExternalLinks(False)

    def _connect_signals(self):
        self.horizontalSlider.valueChanged.connect(self._update_display)
        self.baseRateSpin.valueChanged.connect(self._update_display)
        self.pushButton.clicked.connect(self._apply_rules)
        self.refreshButton.clicked.connect(self._load_interfaces)
        self.ifaceCombo.currentIndexChanged.connect(self._interface_changed)

    def _load_interfaces(self):
        current = self.ifaceCombo.currentText()
        self.ifaceCombo.blockSignals(True)
        self.ifaceCombo.clear()
        items = list_physical_ethernet_interfaces()
        self.ifaceCombo.addItems(items)
        self.ifaceCombo.blockSignals(False)

        if items:
            idx = max(0, items.index(current)) if current in items else 0
            self.ifaceCombo.setCurrentIndex(idx)
        self._interface_changed()

    def _interface_changed(self):
        iface = self.ifaceCombo.currentText()
        if not iface:
            self.detectedSpeedLabel.setText("Detected: none")
            self.linkStateLabel.setText("Link: none")
            return

        linked = has_carrier(iface)
        self.linkStateLabel.setText("Link: connected" if linked else "Link: disconnected")

        negotiated = negotiated_speed_mbps(iface) if linked else None
        guessed = supported_speed_guess_mbps(iface)
        if negotiated:
            self.detectedSpeedLabel.setText(f"Detected: {negotiated} Mbps")
            self.baseRateSpin.setValue(negotiated)
        elif guessed:
            self.detectedSpeedLabel.setText(f"Detected: unavailable (suggest {guessed} Mbps)")
            self.baseRateSpin.setValue(guessed)
        else:
            self.detectedSpeedLabel.setText("Detected: unavailable")

        self._update_display()

    def _update_display(self):
        percent = self.horizontalSlider.value()
        base_mbps = self.baseRateSpin.value()
        applied = max(1, int(round(base_mbps * (percent / 100.0))))
        self.currentRateLabel.setText(f"{percent}% = {applied} Mbps")

    def append_status(self, text: str):
        self.textBrowser.append(text)

    def _apply_rules(self):
        iface = self.ifaceCombo.currentText()
        if not iface:
            QtWidgets.QMessageBox.warning(self, "No interface", "No physical Ethernet interface was found.")
            return

        self.textBrowser.clear()
        percent = self.horizontalSlider.value()
        base_mbps = self.baseRateSpin.value()

        try:
            _, applied = self.shaper.apply(iface, base_mbps, percent, 502)
        except Exception as exc:
            self.append_status("")
            self.append_status(f"ERROR: {exc}")
            QtWidgets.QMessageBox.critical(self, "Failed", str(exc))
            return

        if applied:
            QtWidgets.QMessageBox.information(self, "Applied", "Port 502 limit applied successfully.")
        else:
            QtWidgets.QMessageBox.information(
                self,
                "Dry run",
                "Not running as root, so the commands were listed in the status box instead."
            )


def main():
    ui_path = Path(sys.argv[1]) if len(sys.argv) > 1 else UI_DEFAULT
    if not ui_path.exists():
        print(f"UI file not found: {ui_path}", file=sys.stderr)
        sys.exit(1)

    app = QtWidgets.QApplication(sys.argv)
    dlg = MainDialog(ui_path)
    dlg.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
