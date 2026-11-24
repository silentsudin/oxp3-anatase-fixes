/* SPDX-License-Identifier: MIT
 *
 * fsr4dec.dll - the Compression API (cabinet.dll) decompressor the FSR 4 XMX upscaler needs under
 * Wine, serving its kernel packs from data.bin.
 *
 * OptiScaler's "FSR 4 and DLSS on Intel XMX" build of amd_fidelityfx_upscaler_dx12.dll unpacks its
 * XMX kernels with cabinet.dll's LZMS Decompress(), which Wine does not implement. Its bundle
 * patches the DLL to import this file instead (by ordinal: 40 CreateDecompressor, 43 Decompress,
 * 45). The bundle's own fsr4dec.dll looks the bytes up per 64 KB chunk hash and misses the first
 * pack ("Decompress MISS csize=6107632 cap=35346326"), so the Xe2/Xe3 XMX variants never load and
 * the menu offers only the DLSS-model and 4.0.2X variants.
 *
 * data.bin holds every pack unpacked, back to back; tools/fsr4-xmx/make-packs.py checks each slice
 * against the SHA-256 the upscaler verifies and writes fsr4dec-packs.txt ("<compressed>
 * <unpacked> <offset>" per line). Decompress() reads the unpacked size from the pack's header,
 * finds that pack, and copies its slice. Anything that does not match fails cleanly: the upscaler
 * then reports the pack invalid, as it does on Windows with corrupt data.
 *
 * Both files are looked up next to this DLL. A log goes to fsr4dec.log there.
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>

#define MAX_PACKS 64
#define MAGIC 0xC0E5510Au   /* 0a 51 e5 c0 */

typedef struct { unsigned long long csize, raw, off; } pack_t;

static pack_t packs[MAX_PACKS];
static int npacks = -1;
static WCHAR dir[MAX_PATH];
static CRITICAL_SECTION lock;

static void logf_(const char *fmt, ...)
{
    WCHAR path[MAX_PATH + 32];
    FILE *f;
    va_list ap;
    _snwprintf(path, MAX_PATH + 32, L"%lsfsr4dec.log", dir);
    if (!(f = _wfopen(path, L"a")))
        return;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static void load_packs(void)
{
    WCHAR path[MAX_PATH + 32];
    char line[256];
    FILE *f;
    npacks = 0;
    _snwprintf(path, MAX_PATH + 32, L"%lsfsr4dec-packs.txt", dir);
    if (!(f = _wfopen(path, L"r"))) {
        logf_("--- fsr4dec (oxp3): no fsr4dec-packs.txt next to the DLL; every Decompress fails");
        return;
    }
    while (npacks < MAX_PACKS && fgets(line, sizeof(line), f)) {
        pack_t p;
        if (line[0] == '#')
            continue;
        if (sscanf(line, "%llu %llu %llu", &p.csize, &p.raw, &p.off) == 3)
            packs[npacks++] = p;
    }
    fclose(f);
    logf_("--- fsr4dec (oxp3) loaded: %d packs from fsr4dec-packs.txt", npacks);
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        WCHAR *slash;
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&lock);
        GetModuleFileNameW(inst, dir, MAX_PATH);
        slash = wcsrchr(dir, L'\\');
        if (slash)
            slash[1] = 0;
    }
    return TRUE;
}

/* --- decompressor --------------------------------------------------------------------------- */

BOOL WINAPI CreateDecompressor(DWORD algorithm, void *routines, HANDLE *handle)
{
    (void)routines;
    EnterCriticalSection(&lock);
    if (npacks < 0)
        load_packs();
    LeaveCriticalSection(&lock);
    if (!handle) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    *handle = (HANDLE)(ULONG_PTR)(0x4f585033u);   /* any non-null value: there is no state */
    logf_("CreateDecompressor alg=%lu -> ok", (unsigned long)algorithm);
    return TRUE;
}

BOOL WINAPI Decompress(HANDLE h, const void *in, SIZE_T insize, void *out, SIZE_T outsize, SIZE_T *outdata)
{
    const unsigned char *b = in;
    unsigned long long raw;
    const pack_t *p = NULL;
    WCHAR path[MAX_PATH + 32];
    HANDLE f;
    LARGE_INTEGER pos;
    SIZE_T done = 0;
    int i;
    (void)h;

    if (!in || insize < 16 || *(const DWORD *)b != MAGIC) {
        logf_("Decompress: not a Compression API buffer (csize=%lu)", (unsigned long)insize);
        SetLastError(ERROR_INVALID_DATA);
        return FALSE;
    }
    raw = *(const unsigned long long *)(b + 8);
    for (i = 0; i < npacks; i++)
        if (packs[i].raw == raw) {
            p = &packs[i];
            break;
        }
    if (!p) {
        logf_("Decompress MISS csize=%lu raw=%llu: no such pack in fsr4dec-packs.txt", (unsigned long)insize, raw);
        SetLastError(ERROR_INVALID_DATA);
        return FALSE;
    }
    if (outdata)
        *outdata = (SIZE_T)raw;
    if (!out || outsize < raw) {      /* the size query: the caller allocates and calls again */
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return FALSE;
    }
    _snwprintf(path, MAX_PATH + 32, L"%lsdata.bin", dir);
    f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    if (f == INVALID_HANDLE_VALUE) {
        logf_("Decompress: cannot open data.bin (error %lu)", GetLastError());
        SetLastError(ERROR_INVALID_DATA);
        return FALSE;
    }
    pos.QuadPart = (LONGLONG)p->off;
    if (!SetFilePointerEx(f, pos, NULL, FILE_BEGIN)) {
        CloseHandle(f);
        SetLastError(ERROR_INVALID_DATA);
        return FALSE;
    }
    while (done < raw) {
        DWORD want = (DWORD)((raw - done) > (1u << 30) ? (1u << 30) : (raw - done)), got = 0;
        if (!ReadFile(f, (unsigned char *)out + done, want, &got, NULL) || !got)
            break;
        done += got;
    }
    CloseHandle(f);
    if (done != raw) {
        logf_("Decompress: data.bin short read for pack at %llu (%lu of %llu bytes)", p->off, (unsigned long)done, raw);
        SetLastError(ERROR_INVALID_DATA);
        return FALSE;
    }
    logf_("Decompress HIT csize=%lu raw=%llu offset=%llu", (unsigned long)insize, raw, p->off);
    return TRUE;
}

BOOL WINAPI CloseDecompressor(HANDLE h) { (void)h; return TRUE; }
BOOL WINAPI ResetDecompressor(HANDLE h) { (void)h; return TRUE; }
BOOL WINAPI QueryDecompressorInformation(HANDLE h, DWORD c, void *b, SIZE_T s)
{ (void)h; (void)c; (void)b; (void)s; SetLastError(ERROR_NOT_SUPPORTED); return FALSE; }
BOOL WINAPI SetDecompressorInformation(HANDLE h, DWORD c, const void *b, SIZE_T s)
{ (void)h; (void)c; (void)b; (void)s; return TRUE; }

/* --- compressor: not needed by the upscaler ------------------------------------------------- */

BOOL WINAPI CreateCompressor(DWORD a, void *r, HANDLE *h)
{ (void)a; (void)r; (void)h; SetLastError(ERROR_NOT_SUPPORTED); return FALSE; }
BOOL WINAPI Compress(HANDLE h, const void *i, SIZE_T is, void *o, SIZE_T os, SIZE_T *od)
{ (void)h; (void)i; (void)is; (void)o; (void)os; (void)od; SetLastError(ERROR_NOT_SUPPORTED); return FALSE; }
BOOL WINAPI CloseCompressor(HANDLE h) { (void)h; return TRUE; }
BOOL WINAPI QueryCompressorInformation(HANDLE h, DWORD c, void *b, SIZE_T s)
{ (void)h; (void)c; (void)b; (void)s; SetLastError(ERROR_NOT_SUPPORTED); return FALSE; }
BOOL WINAPI SetCompressorInformation(HANDLE h, DWORD c, const void *b, SIZE_T s)
{ (void)h; (void)c; (void)b; (void)s; return TRUE; }
