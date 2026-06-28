#!/usr/bin/env python3
import urwid
import subprocess
import re
import os
import time
import glob

# -------------------------------
# Basic System Info Functions
# -------------------------------

def get_device_info():
    try:
        device_info = subprocess.check_output(
            "cat /sys/firmware/devicetree/base/compatible",
            shell=True, stderr=subprocess.DEVNULL
        ).decode("utf-8").replace("\x00", "").strip()
    except Exception:
        device_info = "N/A"

    npu_version = ""
    npu_version_path = "/sys/kernel/debug/rknpu/version"
    if os.path.exists(npu_version_path):
        try:
            with open(npu_version_path, "r") as f:
                npu_version = f.read().strip()
        except Exception:
            npu_version = "Check Permission"

    try:
        uptime = subprocess.check_output("uptime -p", shell=True).decode("utf-8").strip()
    except Exception:
        uptime = "N/A"

    docker_status = ""
    try:
        status = subprocess.check_output("systemctl is-active docker", shell=True, stderr=subprocess.PIPE).decode("utf-8").strip()
        docker_status = "Running ✅" if status == "active" else "Not Installed"
    except Exception:
        docker_status = "Not Installed"

    return device_info, npu_version, uptime, docker_status

def get_cpu_info():
    cpu_loads = {}
    core_count = os.cpu_count() or 1
    global prev_cpu
    try:
        with open("/proc/stat", "r") as f:
            lines = f.readlines()
    except Exception:
        lines = []

    for i in range(core_count):
        line = next((l for l in lines if l.startswith(f"cpu{i} ")), None)
        if not line: continue
        parts = line.split()
        try:
            user, nice, system, idle, iowait, irq, softirq, steal = map(int, parts[1:9])
            total = user + nice + system + idle + iowait + irq + softirq + steal
            if i in prev_cpu:
                prev_total, prev_idle = prev_cpu[i]
                diff_total = total - prev_total
                diff_idle = idle - prev_idle
                load = (100 * (diff_total - diff_idle)) // diff_total if diff_total > 0 else 0
            else:
                load = 0
            cpu_loads[i] = load
            prev_cpu[i] = (total, idle)
        except: continue

    cpu_freqs = {}
    for i in range(core_count):
        try:
            with open(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", "r") as f:
                cpu_freqs[i] = int(f.read().strip()) // 1000
        except: cpu_freqs[i] = 0
    return cpu_loads, cpu_freqs

def get_gpu_info():
    gpu_dev = "CIXH5000:00"
    gpu_freq_path = f"/sys/class/devfreq/{gpu_dev}/cur_freq"
    gpu_load_path = f"/sys/class/devfreq/{gpu_dev}/load"

    # 厂商内核逻辑
    if os.path.exists(gpu_load_path):
        try:
            with open(gpu_load_path, "r") as f:
                gpu_load = int(f.read().strip().split('@')[0].rstrip('%'))
            with open(gpu_freq_path, "r") as f:
                gpu_freq = int(f.read().strip()) // 1000000
            return gpu_load, gpu_freq
        except: pass

    # 7.0 内核 + Panthor 驱动逻辑
    if os.path.exists(gpu_freq_path):
        global prev_gpu_busy, prev_gpu_time
        try:
            with open(gpu_freq_path, "r") as f:
                gpu_freq = int(f.read().strip()) // 1000000
            current_busy = 0
            for fdinfo in glob.glob("/proc/*/fdinfo/*"):
                try:
                    with open(fdinfo, "r") as f:
                        data = f.read(1024)
                        if "drm-driver:\tpanthor" in data:
                            m = re.search(r"drm-engine-panthor:\s*(\d+) ns", data)
                            if m: current_busy += int(m.group(1))
                except: continue
            curr_time = time.time()
            if prev_gpu_time > 0:
                dt = curr_time - prev_gpu_time
                gpu_load = min(100, int(((current_busy - prev_gpu_busy) / (dt * 1e9)) * 100)) if dt > 0 else 0
            else: gpu_load = 0
            prev_gpu_busy, prev_gpu_time = current_busy, curr_time
            return gpu_load, gpu_freq
        except: return 0, 0
    return None, None

def get_npu_info():
    npu_load_path = "/sys/kernel/debug/rknpu/load"
    npu_freq_path = "/sys/class/devfreq/fdab0000.npu/cur_freq"
    if not os.path.exists(npu_load_path): return None, None
    try:
        with open(npu_load_path, "r") as f:
            percents = re.findall(r'(\d+)%', f.read())
            npu_load = " ".join([p + "%" for p in percents]) if percents else "0% 0% 0%"
        with open(npu_freq_path, "r") as f:
            npu_freq = int(f.read().strip()) // 1000000
        return npu_load, npu_freq
    except: return "0% 0% 0%", 0

def get_ram_swap_info():
    try:
        out = subprocess.check_output("free -h", shell=True).decode("utf-8").splitlines()
        ram = out[1].split()
        swap = out[2].split()
        return ram[2], ram[1], swap[2], swap[1]
    except: return "N/A", "N/A", "N/A", "N/A"

def get_temperatures():
    # 针对 Orange Pi 6 (CIX-P1) 全面定制的映射字典
    sensor_map = {
        "TZGT": "GPU",
        "TZB0": "CPU (Big Cluster 0)",
        "TZB1": "CPU (Big Cluster 1)",
        "TZM0": "CPU (Mid Cluster 0)",
        "TZM1": "CPU (Mid Cluster 1)",
        "nvme": "NVMe SSD",
        "r8169_0_3100:00": "LAN Port 1 (RTL8169)",
        "r8169_0_6100:00": "LAN Port 2 (RTL8169)",
    }

    # 🌟 新增：强制排序权重字典（数字越小越靠前）
    sort_priority = {
        "GPU": 1,
        "CPU (Big Cluster 0)": 2,
        "CPU (Big Cluster 1)": 3,
        "CPU (Mid Cluster 0)": 4,
        "CPU (Mid Cluster 1)": 5,
        "NVMe SSD": 6,
        "LAN Port 1 (RTL8169)": 7,
        "LAN Port 2 (RTL8169)": 8,
    }

    try:
        output = subprocess.check_output("sensors", shell=True, stderr=subprocess.DEVNULL).decode("utf-8")
        lines = output.splitlines()

        # 改用字典列表来临时存储，方便后续排序
        parsed_items = []
        current_name = None

        for line in lines:
            if not line.strip():
                current_name = None
                continue

            # 识别标题行
            if len(line.split()) == 1:
                raw_name = line.strip()
                clean_name = raw_name.split('-')[0]
                current_name = sensor_map.get(clean_name, raw_name)
                continue

            # 提取温度
            if "temp1" in line or "Composite" in line or "Package id 0" in line:
                fields = line.split()
                if len(fields) < 2: continue
                m = re.search(r'\+([\d\.]+)', fields[1])
                if m:
                    temp_val = int(float(m.group(1)))

                    if temp_val >= 70: attr = 'temp_red'
                    elif temp_val >= 60: attr = 'temp_yellow'
                    else: attr = 'temp_green'

                    sensor_name = current_name if current_name else fields[0]
                    formatted = f"{sensor_name:<30} {temp_val:2d}°C"

                    # 临时把名称、颜色、格式化文本打包存起来
                    parsed_items.append({
                        "name": sensor_name,
                        "attr": attr,
                        "formatted": formatted
                    })

        if not parsed_items:
            return [("default", "No sensors found")]

        # 🌟 核心逻辑：根据 sort_priority 的定义进行排序。
        # 如果遇到未知的传感器（不在列表里），默认给 99 的权重，让它们沉到最底下。
        # 当权重相同时，按照名称的首字母排序。
        parsed_items.sort(key=lambda x: (sort_priority.get(x["name"], 99), x["name"]))

        # 剥离出最终面板需要的元组格式 (attr, formatted_string)
        return [(item["attr"], item["formatted"]) for item in parsed_items]

    except:
        return [("default", "N/A")]

def get_disk_usage():
    try:
        cmd = "findmnt -nt btrfs,ext4,vfat,xfs --output TARGET,SOURCE,FSTYPE,SIZE,USED,AVAIL"
        out = subprocess.check_output(cmd, shell=True).decode("utf-8").splitlines()
        lines = [f"{'Mount Point (Type)':<25} {'Size':>7} {'Used':>7} {'Avail':>7}"]
        for l in out:
            parts = l.split()
            if len(parts) < 6: continue
            target, source, fstype, size, used, avail = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            if target.startswith(('/run/user', '/var/lib/docker')): continue
            if fstype == "btrfs":
                display_name = f"{target} (Btrfs Pool)" if target == "/" else f" ├─{target}"
            else:
                display_name = target
            if len(display_name) > 24: display_name = display_name[:21] + "..."
            lines.append(f"{display_name:<25} {size:>7} {used:>7} {avail:>7}")
        return lines
    except: return ["Disk Info N/A"]

def get_storage_smart(dev):
    try:
        out = subprocess.check_output(f"sudo smartctl -a -d auto /dev/{dev}", shell=True, stderr=subprocess.STDOUT).decode("utf-8")
        model = re.search(r"(?:Device Model|Model Number):\s+(.*)", out)
        temp = re.search(r"(?:Temperature Sensor 1|Temperature_Celsius):\s+(\d+)", out)
        hours = re.search(r"Power On Hours:\s+([\d,]+)", out)
        m_str = model.group(1).strip() if model else "Unknown"
        t_str = f"{temp.group(1)}°C" if temp else ""
        h_str = f"{hours.group(1)}H" if hours else ""
        return f"/dev/{dev} - {m_str} {t_str} {h_str}"
    except: return f"/dev/{dev} - No SMART"

def get_network_traffic():
    global prev_net
    rates = {}
    curr_time = time.time()
    try:
        with open("/proc/net/dev", "r") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                iface = parts[0].strip(':')
                if iface == "lo" or iface.startswith(('br-', 'docker', 'veth')): continue
                rx, tx = int(parts[1]), int(parts[9])
                if iface in prev_net:
                    prx, ptx, pt = prev_net[iface]
                    dt = curr_time - pt
                    rates[iface] = ((rx-prx)*8/(1e6*dt), (tx-ptx)*8/(1e6*dt)) if dt > 0 else (0,0)
                prev_net[iface] = (rx, tx, curr_time)
    except: pass
    return rates

# -------------------------------
# Dashboard Display
# -------------------------------

palette = [
    ('header', 'dark blue,bold', ''),
    ('title', 'yellow,bold', ''),
    ('good', 'light green,bold', ''),
    ('temp_red', 'light red,bold', ''),
    ('temp_yellow', 'yellow,bold', ''),
    ('temp_green', 'light green,bold', ''),
    ('freq', 'light cyan', ''),
    ('footer', 'dark gray', '')
]

def build_dashboard():
    lines = []
    bar = "━" * 60
    lines.append(("header", bar))
    dev, npu_v, uptime, docker = get_device_info()
    lines.append(("title", f" 🚀 PDA OrangePi6 Plus Monitor"))
    lines.append(("default", f" 🕒 Uptime: {uptime}   🐳 Docker: {docker}"))
    lines.append(("header", bar))

    cpu_loads, cpu_freqs = get_cpu_info()
    lines.append(("title", " 📊 CPU Status:"))
    for i in range(0, len(cpu_loads), 2):
        l1, l2 = cpu_loads.get(i, 0), cpu_loads.get(i+1, 0)
        f1, f2 = cpu_freqs.get(i, 0), cpu_freqs.get(i+1, 0)
        markup = [("default", f"  C{i:02d}: "),
                  ('temp_red' if l1>80 else ('temp_yellow' if l1>50 else 'temp_green'), f"{l1:3d}% "), ('freq', f"{f1:4d}MHz  "),
                  ("default", f"C{i+1:02d}: "),
                  ('temp_red' if l2>80 else ('temp_yellow' if l2>50 else 'temp_green'), f"{l2:3d}% "), ('freq', f"{f2:4d}MHz")]
        lines.append(markup)

    lines.append(("header", bar))
    gl, gf = get_gpu_info()
    nl, nf = get_npu_info()
    if gl is not None:
        gpu_attr = 'temp_red' if gl > 80 else ('temp_yellow' if gl > 50 else 'temp_green')
        lines.append([("title", " 🎮 GPU: "), (gpu_attr, f"{gl:3d}%"), ("freq", f"  {gf:4d}MHz")])
    if nl is not None:
        # NPU 负载通常是字符串 "0% 0% 0%"，取第一个值判断颜色
        try:
            n_val = int(re.search(r'(\d+)', nl).group(1))
            npu_attr = 'temp_red' if n_val > 80 else ('temp_yellow' if n_val > 50 else 'temp_green')
        except: npu_attr = 'temp_green'
        lines.append([("title", " 🧠 NPU: "), (npu_attr, f"{nl}"), ("freq", f"  {nf:4d}MHz")])

    lines.append(("header", bar))
    ru, rt, su, st = get_ram_swap_info()
    lines.append(("title", f" 🖥️  RAM: {ru} / {rt}    Swap: {su} / {st}"))

    lines.append(("header", bar))
    temp_items = get_temperatures()
    lines.append(("title", " 🌡️  Temperatures:"))
    for attr, text in temp_items:
        lines.append((attr, f"  {text}"))

    lines.append(("header", bar))
    net = get_network_traffic()
    for iface, (rx, tx) in net.items():
        lines.append(("default", f" 🌐 {iface:<8} ⬇️ {rx:6.2f} Mbps  ⬆️ {tx:6.2f} Mbps"))

    lines.append(("header", bar))
    lines.append(("title", " 💾 Storage Detail (Btrfs Hierarchy):"))
    for dl in get_disk_usage(): lines.append(("default", f"  {dl}"))

    lines.append(("header", bar))
    try:
        for d in os.listdir('/dev'):
            if re.match(r"sd[a-z]$|nvme\dn\d$", d):
                lines.append(("default", f"  {get_storage_smart(d)}"))
    except: pass

    lines.append(("header", bar))
    lines.append(("footer", " [Q] Quit  |  Refresh: 0.5s  |  PDA Advanced Linux System Monitor"))
    return lines

class DashboardWidget(urwid.ListBox):
    def __init__(self):
        self.walker = urwid.SimpleListWalker([])
        super().__init__(self.walker)
        self.update_content()
    def update_content(self):
        self.walker[:] = [urwid.Text(item) for item in build_dashboard()]

def main():
    global prev_cpu, prev_net, prev_gpu_busy, prev_gpu_time
    prev_cpu, prev_net, prev_gpu_busy, prev_gpu_time = {}, {}, 0, 0
    dash = DashboardWidget()
    loop = urwid.MainLoop(dash, palette, unhandled_input=lambda k: k in ('q','Q') and exit_app())
    loop.set_alarm_in(0.1, lambda l, d: periodic_update(l, dash), dash)
    loop.run()

def periodic_update(loop, widget):
    widget.update_content()
    loop.set_alarm_in(0.5, periodic_update, widget)

def exit_app():
    raise urwid.ExitMainLoop()

if __name__ == '__main__':
    main()
