#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdlib.h>

static const char *redir(const char *path, char *buf, size_t n) {
    const char *dir = getenv("OXP3_LIBCLC_DIR");
    if (!path || !dir) return path;
    if (strstr(path, "spirv64-mesa3d-")) { snprintf(buf, n, "%s/spirv64-mesa3d-.spv", dir); return buf; }
    if (strstr(path, "spirv-mesa3d-")) { snprintf(buf, n, "%s/spirv-mesa3d-.spv", dir); return buf; }
    return path;
}
int open(const char *path, int flags, ...) {
    static int (*real)(const char*,int,...); if (!real) real=dlsym(RTLD_NEXT,"open");
    char b[4096]; const char *p=redir(path,b,sizeof b); mode_t m=0;
    if (flags & O_CREAT) { va_list ap; va_start(ap,flags); m=va_arg(ap,int); va_end(ap); }
    return real(p,flags,m);
}
int open64(const char *path, int flags, ...) {
    static int (*real)(const char*,int,...); if (!real) real=dlsym(RTLD_NEXT,"open64");
    char b[4096]; const char *p=redir(path,b,sizeof b); mode_t m=0;
    if (flags & O_CREAT) { va_list ap; va_start(ap,flags); m=va_arg(ap,int); va_end(ap); }
    return real(p,flags,m);
}
int openat(int dirfd, const char *path, int flags, ...) {
    static int (*real)(int,const char*,int,...); if (!real) real=dlsym(RTLD_NEXT,"openat");
    char b[4096]; const char *p=redir(path,b,sizeof b); mode_t m=0;
    if (flags & O_CREAT) { va_list ap; va_start(ap,flags); m=va_arg(ap,int); va_end(ap); }
    return real(dirfd,p,flags,m);
}
int stat(const char *path, struct stat *st) {
    static int (*real)(const char*,struct stat*); if (!real) real=dlsym(RTLD_NEXT,"stat");
    char b[4096]; return real(redir(path,b,sizeof b),st);
}
int stat64(const char *path, struct stat64 *st) {
    static int (*real)(const char*,struct stat64*); if (!real) real=dlsym(RTLD_NEXT,"stat64");
    char b[4096]; return real(redir(path,b,sizeof b),st);
}
int access(const char *path, int mode) {
    static int (*real)(const char*,int); if (!real) real=dlsym(RTLD_NEXT,"access");
    char b[4096]; return real(redir(path,b,sizeof b),mode);
}
