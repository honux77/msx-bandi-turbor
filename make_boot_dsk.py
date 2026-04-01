#!/usr/bin/env python3
"""
Creates a Nextor-bootable MSX-DOS2 720KB floppy disk image (nextor_boot.dsk).

Disk layout (720KB, 80 tracks, 2 sides, 9 sectors/track, 512 bytes/sector):
  Sector 0     : Boot sector (BPB + MSX-DOS2 bootstrap)
  Sectors 1-3  : FAT1
  Sectors 4-6  : FAT2
  Sectors 7-13 : Root directory (112 entries x 32 bytes = 7 sectors)
  Sectors 14+  : Data area

System files placed first:
  NEXTOR.SYS   - hidden/system/read-only, cluster 2 (contiguous)
  COMMAND2.COM - cluster after NEXTOR.SYS
"""

import struct
import sys
import os

# Disk geometry
SECTOR_SIZE       = 512
SECTORS_PER_TRACK = 9
HEADS             = 2
TRACKS            = 80
TOTAL_SECTORS     = TRACKS * HEADS * SECTORS_PER_TRACK  # 1440
SECTORS_PER_CLUSTER = 2
RESERVED_SECTORS  = 1
NUM_FATS          = 2
ROOT_ENTRIES      = 112
SECTORS_PER_FAT   = 3
MEDIA_DESCRIPTOR  = 0xF9

FAT_START         = RESERVED_SECTORS                          # sector 1
ROOT_START        = FAT_START + NUM_FATS * SECTORS_PER_FAT   # sector 7
DATA_START        = ROOT_START + (ROOT_ENTRIES * 32 + SECTOR_SIZE - 1) // SECTOR_SIZE  # sector 14
FIRST_CLUSTER     = 2

def sectors_to_bytes(n):
    return n * SECTOR_SIZE

def cluster_to_sector(cluster):
    return DATA_START + (cluster - FIRST_CLUSTER) * SECTORS_PER_CLUSTER

def make_boot_sector():
    """
    MSX-DOS2 / Nextor compatible boot sector.
    The BPB is standard FAT12. The bootstrap code prints a message when
    booted without the Nextor ROM, and halts. When the Nextor ROM is present,
    the ROM loads NEXTOR.SYS directly and ignores the bootstrap code.
    """
    bs = bytearray(SECTOR_SIZE)

    # --- Jump + OEM ---
    # JMP SHORT to boot code at offset 0x5A
    bs[0] = 0xEB
    bs[1] = 0x58   # jump to offset 0x5A (= 2 + 0x58)
    bs[2] = 0x90   # NOP

    oem = b'NEXTOR  '
    bs[3:11] = oem

    # --- BPB (BIOS Parameter Block) ---
    struct.pack_into('<H', bs, 11, SECTOR_SIZE)
    bs[13] = SECTORS_PER_CLUSTER
    struct.pack_into('<H', bs, 14, RESERVED_SECTORS)
    bs[16] = NUM_FATS
    struct.pack_into('<H', bs, 17, ROOT_ENTRIES)
    struct.pack_into('<H', bs, 19, TOTAL_SECTORS)
    bs[21] = MEDIA_DESCRIPTOR
    struct.pack_into('<H', bs, 22, SECTORS_PER_FAT)
    struct.pack_into('<H', bs, 24, SECTORS_PER_TRACK)
    struct.pack_into('<H', bs, 26, HEADS)
    struct.pack_into('<H', bs, 28, 0)  # hidden sectors

    # --- Bootstrap code at offset 0x5A ---
    # Simple Z80 code: print "Non-system disk" message and wait for keypress
    # BIOS calls: CHPUT = 0x00A2, CHGET = 0x009F
    msg = b'\r\nNon-system disk or disk error\r\nInsert boot disk and press any key\r\n\x00'
    code_offset = 0x5A

    code = bytearray()
    # LD SP, 0xF380    (set up stack in scratch area)
    code += bytes([0x31, 0x80, 0xF3])
    # LD HL, msg_addr  (pointer to message, calculated below)
    code += bytes([0x21, 0x00, 0x00])  # LD HL, placeholder (patched below)
    code += bytes([0xCD, 0x00, 0x00])  # CALL placeholder (patched below)
    # CALL 0x009F (BIOS CHGET - wait for key)
    code += bytes([0xCD, 0x9F, 0x00])
    # JP 0x0000 (warm boot / restart)
    code += bytes([0xC3, 0x00, 0x00])

    # print_str subroutine: print null-terminated string at HL via BIOS CHPUT (0x00A2)
    print_str_offset = code_offset + len(code)
    print_str = bytearray()
    # .loop: LD A,(HL)
    print_str += bytes([0x7E])
    # OR A
    print_str += bytes([0xB7])
    # RET Z
    print_str += bytes([0xC8])
    # CALL 0x00A2 (CHPUT)
    print_str += bytes([0xCD, 0xA2, 0x00])
    # INC HL
    print_str += bytes([0x23])
    # JR .loop  (offset = -8)
    print_str += bytes([0x18, 0xF8])
    code += print_str

    msg_start = code_offset + len(code)
    code += msg

    # Patch LD HL with actual message address (relative to 0x7C00 where boot sector loads)
    # MSX-DOS loads boot sector at 0x7C00
    LOAD_ADDR = 0x7C00
    actual_msg_addr = LOAD_ADDR + msg_start
    struct.pack_into('<H', code, 3, actual_msg_addr)  # patch LD HL operand
    # Patch CALL print_str
    actual_print_str = LOAD_ADDR + print_str_offset
    struct.pack_into('<H', code, 6, actual_print_str)  # patch CALL operand

    # Write code into boot sector
    end = code_offset + len(code)
    if end > SECTOR_SIZE - 2:
        raise RuntimeError(f"Boot code too large: {end} bytes")
    bs[code_offset:code_offset + len(code)] = code

    # Boot sector signature
    bs[510] = 0x55
    bs[511] = 0xAA

    return bytes(bs)


def make_fat(file_sizes):
    """
    Build FAT12 table. file_sizes is a list of file sizes in bytes.
    Files are allocated consecutively starting at cluster 2.
    Returns (fat_bytes, cluster_list) where cluster_list[i] = first cluster of file i.
    """
    fat = bytearray(SECTORS_PER_FAT * SECTOR_SIZE)

    def set_fat_entry(cluster, value):
        offset = cluster * 3 // 2
        if cluster % 2 == 0:
            fat[offset] = value & 0xFF
            fat[offset + 1] = (fat[offset + 1] & 0xF0) | ((value >> 8) & 0x0F)
        else:
            fat[offset] = (fat[offset] & 0x0F) | ((value & 0x0F) << 4)
            fat[offset + 1] = (value >> 4) & 0xFF

    # FAT ID bytes
    fat[0] = MEDIA_DESCRIPTOR
    fat[1] = 0xFF
    fat[2] = 0xFF

    current_cluster = FIRST_CLUSTER
    first_clusters = []

    for size in file_sizes:
        clusters_needed = (size + SECTORS_PER_CLUSTER * SECTOR_SIZE - 1) // (SECTORS_PER_CLUSTER * SECTOR_SIZE)
        first_clusters.append(current_cluster)
        for i in range(clusters_needed):
            if i < clusters_needed - 1:
                set_fat_entry(current_cluster, current_cluster + 1)
            else:
                set_fat_entry(current_cluster, 0xFFF)  # end of chain
            current_cluster += 1

    return bytes(fat), first_clusters


def make_dir_entry(name, ext, attr, first_cluster, file_size):
    """
    Create a 32-byte FAT directory entry.
    attr: 0x01=RO, 0x02=Hidden, 0x04=System, 0x08=VolLabel, 0x10=Dir, 0x20=Archive
    """
    entry = bytearray(32)
    name_b = name.encode('ascii').ljust(8)[:8]
    ext_b  = ext.encode('ascii').ljust(3)[:3]
    entry[0:8]  = name_b
    entry[8:11] = ext_b
    entry[11]   = attr
    # Time: 12:00:00
    struct.pack_into('<H', entry, 22, (12 << 11) | (0 << 5) | 0)
    # Date: 1994-01-01 (year offset from 1980 = 14)
    struct.pack_into('<H', entry, 24, (14 << 9) | (1 << 5) | 1)
    struct.pack_into('<H', entry, 26, first_cluster)
    struct.pack_into('<I', entry, 28, file_size)
    return bytes(entry)


def build_disk(nextor_sys_path, command2_path, output_path):
    with open(nextor_sys_path, 'rb') as f:
        nextor_data = f.read()
    with open(command2_path, 'rb') as f:
        command2_data = f.read()

    print(f"NEXTOR.SYS  : {len(nextor_data):,} bytes")
    print(f"COMMAND2.COM: {len(command2_data):,} bytes")

    disk = bytearray(TOTAL_SECTORS * SECTOR_SIZE)

    # Boot sector
    disk[0:SECTOR_SIZE] = make_boot_sector()

    # FAT
    fat_bytes, first_clusters = make_fat([len(nextor_data), len(command2_data)])
    fat1_start = sectors_to_bytes(FAT_START)
    fat2_start = sectors_to_bytes(FAT_START + SECTORS_PER_FAT)
    disk[fat1_start:fat1_start + len(fat_bytes)] = fat_bytes
    disk[fat2_start:fat2_start + len(fat_bytes)] = fat_bytes

    # Root directory
    root_start = sectors_to_bytes(ROOT_START)
    # NEXTOR.SYS: Hidden + System + ReadOnly = 0x07
    entry0 = make_dir_entry('NEXTOR  ', 'SYS', 0x07, first_clusters[0], len(nextor_data))
    # COMMAND2.COM: Archive = 0x20
    entry1 = make_dir_entry('COMMAND2', 'COM', 0x20, first_clusters[1], len(command2_data))
    disk[root_start:root_start + 32] = entry0
    disk[root_start + 32:root_start + 64] = entry1

    # File data
    def write_file(data, first_cluster):
        cluster = first_cluster
        offset = 0
        while offset < len(data):
            sec = cluster_to_sector(cluster)
            chunk = data[offset:offset + SECTORS_PER_CLUSTER * SECTOR_SIZE]
            pos = sectors_to_bytes(sec)
            disk[pos:pos + len(chunk)] = chunk
            offset += SECTORS_PER_CLUSTER * SECTOR_SIZE
            cluster += 1

    write_file(nextor_data, first_clusters[0])
    write_file(command2_data, first_clusters[1])

    with open(output_path, 'wb') as f:
        f.write(disk)

    print(f"Created: {output_path} ({len(disk):,} bytes)")


if __name__ == '__main__':
    base = os.path.dirname(os.path.abspath(__file__))
    nextor_sys  = os.path.join(base, 'NEXTOR.SYS')
    command2    = os.path.join(base, 'COMMAND2.COM')
    output      = os.path.join(base, 'nextor_boot.dsk')

    for path in [nextor_sys, command2]:
        if not os.path.exists(path):
            print(f"ERROR: Missing file: {path}", file=sys.stderr)
            sys.exit(1)

    build_disk(nextor_sys, command2, output)
