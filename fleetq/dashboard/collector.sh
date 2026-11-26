#!/bin/bash
# Runs ON a remote machine over SSH and prints a single JSON object with stats.
# Supports macOS and Linux (Ubuntu). Uses only tools present on a stock install.

j() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\n'; }   # json-escape
num() { local v="${1:-0}"; [[ "$v" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && printf '%s' "$v" || printf '0'; }

OS=$(uname -s)
HOST=$(hostname -s 2>/dev/null || hostname)
USER_NAME=$(whoami)
ARCH=$(uname -m)
KERNEL=$(uname -r)
NOW=$(date +%s)
PCORES=0; ECORES=0; MEM_FREE_PCT=""; SERIAL=""

if [ "$OS" = "Darwin" ]; then
  sv() { sysctl -n "$1" 2>/dev/null; }
  PLATFORM="macOS"
  MODEL=$(sv hw.model)
  CHIP=$(sv machdep.cpu.brand_string)
  NCPU=$(sv hw.ncpu)
  PCORES=$(sv hw.perflevel0.physicalcpu); PCORES=${PCORES:-0}
  ECORES=$(sv hw.perflevel1.physicalcpu); ECORES=${ECORES:-0}
  MEM_TOTAL=$(sv hw.memsize)
  PAGE=$(sv hw.pagesize)
  OS_NAME="macOS"
  OS_VER=$(sw_vers -productVersion 2>/dev/null)
  OS_BUILD=$(sw_vers -buildVersion 2>/dev/null)
  SERIAL=$(ioreg -c IOPlatformExpertDevice -d 2 2>/dev/null | awk -F'"' '/IOPlatformSerialNumber/{print $4}')
  IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)

  VM=$(vm_stat 2>/dev/null)
  pg() { echo "$VM" | awk -v k="$1" -F: '$1 ~ k {gsub(/[^0-9]/,"",$2); print $2; exit}'; }
  P_FREE=$(pg "^Pages free"); P_SPEC=$(pg "speculative"); P_ACTIVE=$(pg "^Pages active")
  P_INACTIVE=$(pg "^Pages inactive"); P_WIRED=$(pg "wired down"); P_COMP=$(pg "occupied by compressor")
  MEM_FREE=$(( (${P_FREE:-0} + ${P_SPEC:-0}) * PAGE ))
  MEM_INACTIVE=$(( ${P_INACTIVE:-0} * PAGE ))
  MEM_USED=$(( (${P_ACTIVE:-0} + ${P_WIRED:-0} + ${P_COMP:-0}) * PAGE ))
  MEM_AVAILABLE=$(( MEM_FREE + MEM_INACTIVE ))
  MEM_FREE_PCT=$(memory_pressure 2>/dev/null | awk -F': ' '/free percentage/{gsub(/%/,"",$2); print $2}')
  SWAP=$(sv vm.swapusage)
  SWAP_TOTAL_MB=$(echo "$SWAP" | awk '{print $3}' | tr -d 'M'); SWAP_USED_MB=$(echo "$SWAP" | awk '{print $6}' | tr -d 'M')

  DISK_VOL=/System/Volumes/Data; [ -d "$DISK_VOL" ] || DISK_VOL=/
  LOAD=$(sv vm.loadavg | tr -d '{}' | awk '{print $1","$2","$3}')
  BOOT=$(sv kern.boottime | sed -E 's/^\{ sec = ([0-9]+),.*/\1/')
  UPTIME=$(( NOW - BOOT ))

else
  PLATFORM="linux"
  . /etc/os-release 2>/dev/null
  OS_NAME="${NAME:-Linux}"
  OS_VER="${VERSION_ID:-}"
  OS_BUILD="$KERNEL"
  VENDOR=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null); PRODUCT=$(cat /sys/class/dmi/id/product_name 2>/dev/null)
  MODEL=$(echo "$VENDOR $PRODUCT" | sed 's/^ *//;s/ *$//')
  SERIAL=$(cat /sys/class/dmi/id/product_serial 2>/dev/null)   # usually root-only; empty otherwise
  CHIP=$(awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo 2>/dev/null | sed -E 's/\(R\)|\(TM\)//g; s/ +/ /g; s/ CPU @.*//')
  NCPU=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo)
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')

  mi() { awk -v k="$1" '$1==k":"{print $2*1024; exit}' /proc/meminfo; }   # kB -> bytes
  MEM_TOTAL=$(mi MemTotal); MEM_FREE=$(mi MemFree); MEM_AVAILABLE=$(mi MemAvailable); MEM_INACTIVE=$(mi Inactive)
  [ -z "$MEM_AVAILABLE" ] && MEM_AVAILABLE=$MEM_FREE
  MEM_USED=$(( MEM_TOTAL - MEM_AVAILABLE ))
  MEM_FREE_PCT=$(( MEM_TOTAL > 0 ? MEM_AVAILABLE * 100 / MEM_TOTAL : 0 ))
  ST=$(mi SwapTotal); SF=$(mi SwapFree)
  SWAP_TOTAL_MB=$(( ${ST:-0} / 1048576 )); SWAP_USED_MB=$(( (${ST:-0} - ${SF:-0}) / 1048576 ))

  DISK_VOL=/
  LOAD=$(awk '{print $1","$2","$3}' /proc/loadavg)
  UPTIME=$(awk '{printf "%d", $1}' /proc/uptime)
fi

read -r DISK_TOTAL DISK_AVAIL <<< "$(df -kP "$DISK_VOL" | awk 'NR==2{print $2*1024, $4*1024}')"
DISK_USED=$(( DISK_TOTAL - DISK_AVAIL ))

CPU_PCT=$(ps -A -o %cpu | awk -v n="$NCPU" 'NR>1{s+=$1} END{printf "%.1f", (n>0? s/n : 0)}')
NPROC=$(ps -A | awk 'NR>1' | wc -l | tr -d ' ')

PY=$(command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 | awk '{print $2}')
NODE=$(command -v node >/dev/null 2>&1 && node --version 2>/dev/null | tr -d 'v')
DOCKER=$(command -v docker >/dev/null 2>&1 && echo true || echo false)

printf '{'
printf '"platform":"%s","hostname":"%s","user":"%s","model":"%s","chip":"%s","arch":"%s","serial":"%s","ip":"%s",' \
  "$PLATFORM" "$(j "$HOST")" "$(j "$USER_NAME")" "$(j "$MODEL")" "$(j "$CHIP")" "$(j "$ARCH")" "$(j "$SERIAL")" "$(j "$IP")"
printf '"os":{"name":"%s","version":"%s","build":"%s","kernel":"%s"},' "$(j "$OS_NAME")" "$(j "$OS_VER")" "$(j "$OS_BUILD")" "$(j "$KERNEL")"
printf '"cpu":{"cores":%s,"perf_cores":%s,"eff_cores":%s,"usage_pct":%s,"load":[%s],"processes":%s},' \
  "$(num "$NCPU")" "$(num "$PCORES")" "$(num "$ECORES")" "$(num "$CPU_PCT")" "${LOAD:-0,0,0}" "$(num "$NPROC")"
printf '"memory":{"total":%s,"used":%s,"free":%s,"inactive":%s,"available":%s,"free_pct":%s,"swap_total_mb":%s,"swap_used_mb":%s},' \
  "$(num "$MEM_TOTAL")" "$(num "$MEM_USED")" "$(num "$MEM_FREE")" "$(num "$MEM_INACTIVE")" "$(num "$MEM_AVAILABLE")" "$(num "$MEM_FREE_PCT")" "$(num "$SWAP_TOTAL_MB")" "$(num "$SWAP_USED_MB")"
printf '"disk":{"total":%s,"used":%s,"available":%s},' "$(num "$DISK_TOTAL")" "$(num "$DISK_USED")" "$(num "$DISK_AVAIL")"
printf '"uptime_s":%s,' "$(num "$UPTIME")"
printf '"runtimes":{"python":"%s","node":"%s","docker":%s},' "$(j "$PY")" "$(j "$NODE")" "$DOCKER"
printf '"collected_at":%s' "$NOW"
printf '}\n'
