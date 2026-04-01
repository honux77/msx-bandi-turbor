#!/usr/bin/env python3
"""
Nextor 부팅 가능한 IDE HDD 이미지 생성 (bandi_iq100r_hd.dsk)

구조:
  Sector 0       : MBR (Nextor/MSX IDE 호환 파티션 테이블)
  Sector 1       : FAT16 파티션 부트 섹터 (BPB)
  Sector 2-33    : FAT1 + FAT2
  Sector 34-65   : 루트 디렉터리 (512 entries)
  Sector 66+     : 데이터 영역

파티션 1 (FAT16): 전체 디스크 사용 (100MB)
"""

import struct
import os

# 디스크 설정 (100MB)
HDD_MB           = 100
SECTOR_SIZE      = 512
TOTAL_SECTORS    = (HDD_MB * 1024 * 1024) // SECTOR_SIZE  # 204800

# FAT16 BPB 설정
RESERVED_SECTORS   = 1     # 부트 섹터
NUM_FATS           = 2
SECTORS_PER_FAT    = 16    # FAT16 (충분히 크게)
ROOT_ENTRIES       = 512   # HDD는 512 엔트리
SECTORS_PER_CLUSTER = 4    # 2KB 클러스터
PARTITION_START    = 1     # MBR 다음 섹터

# 계산
ROOT_SECTORS = (ROOT_ENTRIES * 32 + SECTOR_SIZE - 1) // SECTOR_SIZE  # 32
FAT_START    = PARTITION_START + RESERVED_SECTORS                     # sector 2
ROOT_START   = FAT_START + NUM_FATS * SECTORS_PER_FAT                # sector 34
DATA_START   = ROOT_START + ROOT_SECTORS                              # sector 66
FIRST_CLUSTER = 2

PARTITION_SECTORS = TOTAL_SECTORS - PARTITION_START


def lba_to_chs(lba, heads=16, spt=63):
    c = lba // (heads * spt)
    h = (lba // spt) % heads
    s = (lba % spt) + 1
    if c > 1023: c, h, s = 1023, 254, 63
    return c, h, s


def chs_bytes(c, h, s):
    return bytes([h, (s & 0x3F) | ((c >> 2) & 0xC0), c & 0xFF])


def make_mbr():
    mbr = bytearray(SECTOR_SIZE)
    # 부트 코드: 파티션 1로 점프 (간단한 MBR stub)
    mbr[0] = 0xFA  # CLI
    mbr[1] = 0xEB  # JMP $
    mbr[2] = 0xFE

    # 파티션 엔트리 1 (offset 446)
    entry = bytearray(16)
    entry[0] = 0x80  # 부팅 가능

    cs, hs, ss = lba_to_chs(PARTITION_START)
    entry[1:4] = chs_bytes(cs, hs, ss)

    entry[4] = 0x06  # FAT16 (>32MB)

    last_lba = PARTITION_START + PARTITION_SECTORS - 1
    ce, he, se = lba_to_chs(last_lba)
    entry[5:8] = chs_bytes(ce, he, se)

    struct.pack_into('<I', entry, 8, PARTITION_START)
    struct.pack_into('<I', entry, 12, PARTITION_SECTORS)

    mbr[446:462] = entry
    mbr[510] = 0x55
    mbr[511] = 0xAA
    return bytes(mbr)


def make_boot_sector():
    bs = bytearray(SECTOR_SIZE)
    # JMP + NOP
    bs[0] = 0xEB; bs[1] = 0x58; bs[2] = 0x90
    # OEM
    bs[3:11] = b'NEXTOR  '
    # BPB
    struct.pack_into('<H', bs, 11, SECTOR_SIZE)
    bs[13] = SECTORS_PER_CLUSTER
    struct.pack_into('<H', bs, 14, RESERVED_SECTORS)
    bs[16] = NUM_FATS
    struct.pack_into('<H', bs, 17, ROOT_ENTRIES)
    struct.pack_into('<H', bs, 19, 0)           # 0 = 섹터 수가 32비트에 있음
    bs[21] = 0xF8                               # 고정 미디어
    struct.pack_into('<H', bs, 22, SECTORS_PER_FAT)
    struct.pack_into('<H', bs, 24, 63)          # spt
    struct.pack_into('<H', bs, 26, 16)          # heads
    struct.pack_into('<I', bs, 28, PARTITION_START)  # hidden sectors
    struct.pack_into('<I', bs, 32, PARTITION_SECTORS)
    # FAT16 extended BPB
    bs[36] = 0x80          # drive number
    bs[37] = 0x00
    bs[38] = 0x29          # extended boot signature
    struct.pack_into('<I', bs, 39, 0x19940101)  # volume serial
    bs[43:54] = b'BANDI IQ100'
    bs[54:62] = b'FAT16   '
    bs[510] = 0x55
    bs[511] = 0xAA
    return bytes(bs)


def make_fat16(file_sizes):
    fat = bytearray(SECTORS_PER_FAT * SECTOR_SIZE)
    # FAT16 미디어 바이트 + 0xFFFF
    struct.pack_into('<H', fat, 0, 0xFFF8)
    struct.pack_into('<H', fat, 2, 0xFFFF)

    current = FIRST_CLUSTER
    starts = []
    for size in file_sizes:
        n = (size + SECTORS_PER_CLUSTER * SECTOR_SIZE - 1) \
            // (SECTORS_PER_CLUSTER * SECTOR_SIZE)
        starts.append(current)
        for i in range(n):
            val = current + 1 if i < n - 1 else 0xFFFF
            struct.pack_into('<H', fat, current * 2, val)
            current += 1
    return bytes(fat), starts


def make_dir_entry(name8, ext3, attr, cluster, size):
    e = bytearray(32)
    e[0:8]  = name8.encode().ljust(8)[:8]
    e[8:11] = ext3.encode().ljust(3)[:3]
    e[11]   = attr
    struct.pack_into('<H', e, 22, (12 << 11))   # 12:00:00
    struct.pack_into('<H', e, 24, (14 << 9) | (1 << 5) | 1)  # 1994-01-01
    struct.pack_into('<H', e, 26, cluster)
    struct.pack_into('<I', e, 28, size)
    return bytes(e)


def cluster_to_abs_sector(cluster):
    """파티션 내 클러스터 → 디스크 절대 섹터"""
    return PARTITION_START + DATA_START - PARTITION_START \
        + (cluster - FIRST_CLUSTER) * SECTORS_PER_CLUSTER


def build_hdd(files, output_path):
    """
    files: list of (name8, ext3, attr, path)
      attr 0x07 = 시스템 파일 (NEXTOR.SYS, MSXDOS.SYS 등)
      attr 0x20 = 일반 파일
    """
    file_data = []
    for name, ext, attr, path in files:
        with open(path, 'rb') as f:
            data = f.read()
        file_data.append((name, ext, attr, data))
        print(f"  {name}.{ext:<3}  {len(data):>8,} bytes  attr=0x{attr:02X}")

    disk = bytearray(TOTAL_SECTORS * SECTOR_SIZE)

    # MBR
    disk[0:SECTOR_SIZE] = make_mbr()

    # 파티션 부트 섹터 (sector 1 = PARTITION_START)
    disk[PARTITION_START * SECTOR_SIZE:(PARTITION_START + 1) * SECTOR_SIZE] = make_boot_sector()

    # FAT (파티션 내 sector 1 ~ 1+NUM_FATS*SECTORS_PER_FAT)
    fat_bytes, starts = make_fat16([len(d) for _, _, _, d in file_data])
    fat1_abs = (PARTITION_START + RESERVED_SECTORS) * SECTOR_SIZE
    fat2_abs = fat1_abs + SECTORS_PER_FAT * SECTOR_SIZE
    disk[fat1_abs:fat1_abs + len(fat_bytes)] = fat_bytes
    disk[fat2_abs:fat2_abs + len(fat_bytes)] = fat_bytes

    # 루트 디렉터리
    root_abs = (PARTITION_START + RESERVED_SECTORS + NUM_FATS * SECTORS_PER_FAT) * SECTOR_SIZE
    for i, (name, ext, attr, data) in enumerate(file_data):
        entry = make_dir_entry(name, ext, attr, starts[i], len(data))
        disk[root_abs + i * 32:root_abs + (i + 1) * 32] = entry

    # 파일 데이터
    data_abs_base = (PARTITION_START + RESERVED_SECTORS
                     + NUM_FATS * SECTORS_PER_FAT + ROOT_SECTORS) * SECTOR_SIZE

    for i, (_, _, _, data) in enumerate(file_data):
        cluster = starts[i]
        offset = 0
        while offset < len(data):
            sec_in_partition = DATA_START - PARTITION_START + \
                (cluster - FIRST_CLUSTER) * SECTORS_PER_CLUSTER
            abs_pos = (PARTITION_START + sec_in_partition) * SECTOR_SIZE
            chunk = data[offset:offset + SECTORS_PER_CLUSTER * SECTOR_SIZE]
            disk[abs_pos:abs_pos + len(chunk)] = chunk
            offset += SECTORS_PER_CLUSTER * SECTOR_SIZE
            cluster += 1

    with open(output_path, 'wb') as f:
        f.write(disk)
    print(f"\n완료: {output_path} ({len(disk):,} bytes / {len(disk)//1024//1024}MB)")


if __name__ == '__main__':
    base = os.path.dirname(os.path.abspath(__file__))
    output = os.path.join(base, 'bandi_iq100r_hd.dsk')

    print("Nextor HDD 이미지 생성 중...")
    # 시스템 파일 먼저 (attr=0x07), 나머지는 archive (attr=0x20)
    files = [
        ('NEXTOR  ', 'SYS', 0x07, os.path.join(base, 'NEXTOR.SYS')),
        ('MSXDOS2 ', 'SYS', 0x07, os.path.join(base, 'MSXDOS2.SYS')),
        ('MSXDOS  ', 'SYS', 0x07, os.path.join(base, 'MSXDOS.SYS')),
        ('COMMAND2', 'COM', 0x20, os.path.join(base, 'COMMAND2.COM')),
        ('COMMAND ', 'COM', 0x20, os.path.join(base, 'COMMAND.COM')),
        ('IDEPAR  ', 'COM', 0x20, os.path.join(base, 'IDEPAR.COM')),
        ('MAPDRV  ', 'COM', 0x20, os.path.join(base, 'MAPDRV.COM')),
        ('DRVINFO ', 'COM', 0x20, os.path.join(base, 'DRVINFO.COM')),
    ]

    missing = [p for _, _, _, p in files if not os.path.exists(p)]
    if missing:
        for p in missing: print(f"ERROR: {p}")
        raise SystemExit(1)

    build_hdd(files, output)
