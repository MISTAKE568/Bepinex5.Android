#define _GNU_SOURCE
#include <jni.h>
#include <android/log.h>
#include <android/asset_manager.h>
#include <android/asset_manager_jni.h>
#include <dlfcn.h>
#include <errno.h>
#include <elf.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include "bootstrap_gate.h"
#include "asset_copy.h"

#define LOG_TAG "be5"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

static char g_data_dir[512];
static char g_core_dir[512];

static pid_t tid_now(void) { return (pid_t)syscall(SYS_gettid); }

// Resolve Mono symbols at runtime so the shim uses the game's loaded runtime.
typedef void *MonoDomain;
typedef void *MonoAssembly;
typedef void *MonoImage;
typedef void *MonoClass;
typedef void *MonoMethod;
typedef void *MonoObject;
typedef void *MonoThread;

typedef MonoObject *(*mono_runtime_invoke_t)(MonoMethod *, void *, void **, MonoObject **);
typedef void *(*mono_object_unbox_t)(MonoObject *);
typedef MonoDomain *(*mono_domain_get_t)(void);
typedef MonoThread *(*mono_thread_attach_t)(MonoDomain *);
typedef MonoAssembly *(*mono_domain_assembly_open_t)(MonoDomain *, const char *);
typedef MonoImage *(*mono_assembly_get_image_t)(MonoAssembly *);
typedef MonoClass *(*mono_class_from_name_t)(MonoImage *, const char *, const char *);
typedef MonoMethod *(*mono_class_get_method_from_name_t)(MonoClass *, const char *, int);
typedef const char *(*mono_method_get_name_t)(MonoMethod *);
typedef MonoDomain *(*mono_jit_init_version_t)(const char *root_domain_name,
                                               const char *runtime_version,
                                               const char *runtime_dir);

static void *g_mono;
static mono_runtime_invoke_t g_real_invoke;
static mono_runtime_invoke_t g_real_invoke_array;
static mono_jit_init_version_t g_real_jit_init_version;
static mono_method_get_name_t p_method_name;
static mono_object_unbox_t p_object_unbox;

static be5_bootstrap_gate g_bootstrap_gate;

typedef struct {
    const char *name;
    uintptr_t base;
    const ElfW(Phdr) *phdr;
    int phnum;
} lib_lookup;

static int lib_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    lib_lookup *l = (lib_lookup *)data;
    if (!info->dlpi_name || !info->dlpi_name[0]) return 0;
    const char *b = strrchr(info->dlpi_name, '/');
    b = b ? b + 1 : info->dlpi_name;
    if (strcmp(b, l->name) == 0) {
        l->base = (uintptr_t)info->dlpi_addr;
        l->phdr = info->dlpi_phdr;
        l->phnum = info->dlpi_phnum;
        return 1;
    }
    return 0;
}

static int lib_base(const char *name, uintptr_t *out_base, const ElfW(Phdr) **out_phdr, int *out_phnum) {
    lib_lookup l = { .name = name };
    if (dl_iterate_phdr(lib_cb, &l) == 0) return -1;
    *out_base = l.base;
    *out_phdr = l.phdr;
    *out_phnum = l.phnum;
    return 0;
}

static int find_got_slot(const char *libname, const char *symname, void ***out_slot) {
    uintptr_t base = 0;
    const ElfW(Phdr) *phdr = NULL;
    int phnum = 0;
    if (lib_base(libname, &base, &phdr, &phnum) != 0) return -1;

    ElfW(Dyn) *dyn = NULL;
    for (int i = 0; i < phnum; i++) {
        if (phdr[i].p_type == PT_DYNAMIC) {
            dyn = (ElfW(Dyn) *)(base + phdr[i].p_vaddr);
            break;
        }
    }
    if (!dyn) return -2;

    ElfW(Sym) *symtab = NULL;
    const char *strtab = NULL;
    ElfW(Rel) *jmprel = NULL;
    size_t pltrelsz = 0;
    ElfW(Rel) *rel = NULL;
    size_t relsz = 0;

    for (ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        switch (d->d_tag) {
            case DT_SYMTAB: symtab = (ElfW(Sym) *)(base + d->d_un.d_ptr); break;
            case DT_STRTAB: strtab = (const char *)(base + d->d_un.d_ptr); break;
            case DT_JMPREL: jmprel = (ElfW(Rel) *)(base + d->d_un.d_ptr); break;
            case DT_PLTRELSZ: pltrelsz = d->d_un.d_val; break;
            case DT_REL: rel = (ElfW(Rel) *)(base + d->d_un.d_ptr); break;
            case DT_RELSZ: relsz = d->d_un.d_val; break;
        }
    }
    if (!symtab || !strtab) return -3;

    if (jmprel) {
        for (size_t off = 0; off + sizeof(ElfW(Rel)) <= pltrelsz; off += sizeof(ElfW(Rel))) {
            ElfW(Rel) *r = (ElfW(Rel) *)((char *)jmprel + off);
            size_t symidx = ELF32_R_SYM(r->r_info);
            unsigned type = ELF32_R_TYPE(r->r_info);
            if (type != R_ARM_JUMP_SLOT && type != R_ARM_GLOB_DAT) continue;
            const char *n = strtab + symtab[symidx].st_name;
            if (strcmp(n, symname) == 0) {
                *out_slot = (void **)(base + r->r_offset);
                return 0;
            }
        }
    }
    if (rel) {
        for (size_t off = 0; off + sizeof(ElfW(Rel)) <= relsz; off += sizeof(ElfW(Rel))) {
            ElfW(Rel) *r = (ElfW(Rel) *)((char *)rel + off);
            size_t symidx = ELF32_R_SYM(r->r_info);
            unsigned type = ELF32_R_TYPE(r->r_info);
            if (type != R_ARM_GLOB_DAT && type != R_ARM_JUMP_SLOT) continue;
            const char *n = strtab + symtab[symidx].st_name;
            if (strcmp(n, symname) == 0) {
                *out_slot = (void **)(base + r->r_offset);
                return 0;
            }
        }
    }
    return -4;
}

static int patch_got(const char *libname, const char *symname, void *replacement) {
    void **slot = NULL;
    if (find_got_slot(libname, symname, &slot) != 0 || !slot) {
        LOGW("GOT slot not found: %s in %s", symname, libname);
        return -1;
    }
    long page = sysconf(_SC_PAGESIZE);
    // mprotect requires a page-aligned address, even when only one GOT slot is changed.
    void *pageaddr = (void *)((uintptr_t)slot & ~(uintptr_t)(page - 1));
    if (mprotect(pageaddr, (size_t)page, PROT_READ | PROT_WRITE) != 0) {
        LOGE("mprotect failed for %s in %s", symname, libname);
        return -1;
    }
    void *old = *slot;
    *slot = replacement;
    __builtin___clear_cache((char *)slot, (char *)slot + sizeof(void *));
    LOGI("GOT hook %s in %s: slot=%p old=%p new=%p", symname, libname, (void *)slot, old, replacement);
    return 0;
}

// Cached function pointers bypass dlsym, so they must be replaced in Unity's writable segments.
static int scan_replace_pointer(const char *libname, void *value, void *replacement) {
    uintptr_t base = 0;
    const ElfW(Phdr) *phdr = NULL;
    int phnum = 0;
    if (lib_base(libname, &base, &phdr, &phnum) != 0) return -1;
    long page = sysconf(_SC_PAGESIZE);
    int hits = 0;
    for (int i = 0; i < phnum; i++) {
        if (phdr[i].p_type != PT_LOAD) continue;
        if (!(phdr[i].p_flags & PF_W)) continue;
        uintptr_t start = base + phdr[i].p_vaddr;
        uintptr_t end = start + phdr[i].p_memsz;
        // Pointer reads require native alignment.
        start = (start + sizeof(void *) - 1) & ~(uintptr_t)(sizeof(void *) - 1);
        for (uintptr_t p = start; p + sizeof(void *) <= end; p += sizeof(void *)) {
            if (*(void **)p == value) {
                void *pageaddr = (void *)(p & ~(uintptr_t)(page - 1));
                if (mprotect(pageaddr, (size_t)page, PROT_READ | PROT_WRITE) != 0) continue;
                *(void **)p = replacement;
                hits++;
            }
        }
    }
    if (hits) LOGI("scan_replace %s: replaced %d pointer(s) %p -> %p", libname, hits, value, replacement);
    return hits;
}

typedef void *(*dlsym_fn)(void *, const char *);
typedef void *(*dlopen_fn)(const char *, int);
static dlsym_fn g_dlsym;
static dlopen_fn g_dlopen;

static MonoObject *my_runtime_invoke(MonoMethod *method, void *obj, void **params, MonoObject **exc);
static MonoObject *my_runtime_invoke_array(MonoMethod *method, void *obj, void *params, MonoObject **exc);
static MonoDomain *my_jit_init_version(const char *root_domain_name,
                                       const char *runtime_version,
                                       const char *runtime_dir);
static void *my_dlsym(void *handle, const char *name);
static void *my_dlopen(const char *name, int flags);

static void resolve_helpers(void) {
    if (g_mono) return;
    // Reuse Unity's runtime instead of loading another Mono instance.
    g_mono = dlopen("libmonobdwgc-2.0.so", RTLD_NOLOAD | RTLD_LAZY);
    if (!g_mono) {
        LOGE("resolve_helpers: mono lib not loaded yet");
        return;
    }
    p_method_name = (mono_method_get_name_t)dlsym(g_mono, "mono_method_get_name");
    p_object_unbox = (mono_object_unbox_t)dlsym(g_mono, "mono_object_unbox");
    LOGI("Mono runtime resolved");
}

static void hook_libunity(void) {
    resolve_helpers();
    if (g_mono) {
        void *ri = dlsym(g_mono, "mono_runtime_invoke");
        void *ria = dlsym(g_mono, "mono_runtime_invoke_array");
        void *jit = dlsym(g_mono, "mono_jit_init_version");
        LOGI("hook_libunity: mono_runtime_invoke=%p array=%p jit_init_version=%p", ri, ria, jit);
        if (ri) {
            if (!g_real_invoke) g_real_invoke = (mono_runtime_invoke_t)ri;
            scan_replace_pointer("libunity.so", ri, (void *)my_runtime_invoke);
        }
        if (ria) {
            if (!g_real_invoke_array) g_real_invoke_array = (mono_runtime_invoke_t)ria;
            scan_replace_pointer("libunity.so", ria, (void *)my_runtime_invoke_array);
        }
        if (jit) {
            if (!g_real_jit_init_version) g_real_jit_init_version = (mono_jit_init_version_t)jit;
            scan_replace_pointer("libunity.so", jit, (void *)my_jit_init_version);
        }
    }
    // Intercept future symbol lookups and library loads as well as cached pointers.
    patch_got("libunity.so", "dlsym", (void *)my_dlsym);
    patch_got("libunity.so", "dlopen", (void *)my_dlopen);
}

static void *my_dlsym(void *handle, const char *name) {
    void *real = g_dlsym(handle, name);
    if (!name) return real;
    if (strcmp(name, "mono_runtime_invoke") == 0) {
        if (real && !g_real_invoke) g_real_invoke = (mono_runtime_invoke_t)real;
        LOGI("dlsym(mono_runtime_invoke) -> wrapper (real=%p)", real);
        return (void *)my_runtime_invoke;
    }
    if (strcmp(name, "mono_runtime_invoke_array") == 0) {
        if (real && !g_real_invoke_array) g_real_invoke_array = (mono_runtime_invoke_t)real;
        LOGI("dlsym(mono_runtime_invoke_array) -> wrapper (real=%p)", real);
        return (void *)my_runtime_invoke_array;
    }
    if (strcmp(name, "mono_jit_init_version") == 0) {
        if (real && !g_real_jit_init_version) g_real_jit_init_version = (mono_jit_init_version_t)real;
        LOGI("dlsym(mono_jit_init_version) -> wrapper (real=%p)", real);
        return (void *)my_jit_init_version;
    }
    return real;
}

static void *my_dlopen(const char *name, int flags) {
    void *h = g_dlopen(name, flags);
    if (name && h) {
        if (strstr(name, "libunity.so")) {
            LOGI("dlopen(%s) -> %p, hooking libunity", name, h);
            hook_libunity();
        } else if (strstr(name, "libmonobdwgc")) {
            LOGI("dlopen(%s) -> %p (mono loaded)", name, h);
            resolve_helpers();
        }
    }
    return h;
}

static int call_boot_method(const char *method_name) {
    resolve_helpers();
    if (!g_mono) {
        LOGE("bootstrap[%s]: mono not loaded", method_name);
        return 10;
    }
    mono_domain_get_t domain_get = (mono_domain_get_t)dlsym(g_mono, "mono_domain_get");
    mono_thread_attach_t thread_attach = (mono_thread_attach_t)dlsym(g_mono, "mono_thread_attach");
    mono_domain_assembly_open_t asm_open = (mono_domain_assembly_open_t)dlsym(g_mono, "mono_domain_assembly_open");
    mono_assembly_get_image_t asm_image = (mono_assembly_get_image_t)dlsym(g_mono, "mono_assembly_get_image");
    mono_class_from_name_t class_from_name = (mono_class_from_name_t)dlsym(g_mono, "mono_class_from_name");
    mono_class_get_method_from_name_t method_from_name = (mono_class_get_method_from_name_t)dlsym(g_mono, "mono_class_get_method_from_name");
    // Unity may not have resolved runtime_invoke yet when the Mono domain is initialized.
    mono_runtime_invoke_t runtime_invoke = g_real_invoke;
    if (!runtime_invoke)
        runtime_invoke = (mono_runtime_invoke_t)dlsym(g_mono, "mono_runtime_invoke");

    if (!domain_get || !asm_open || !asm_image || !class_from_name || !method_from_name || !runtime_invoke || !p_object_unbox) {
        LOGE("bootstrap[%s]: missing mono api (domain=%p open=%p image=%p class=%p method=%p invoke=%p unbox=%p)",
             method_name,
             (void *)domain_get, (void *)asm_open, (void *)asm_image,
             (void *)class_from_name, (void *)method_from_name, (void *)runtime_invoke,
             (void *)p_object_unbox);
        return 11;
    }

    MonoDomain *domain = domain_get();
    if (!domain) {
        LOGE("bootstrap[%s]: mono domain unavailable", method_name);
        return 15;
    }
    if (thread_attach) thread_attach(domain);
    LOGI("bootstrap[%s]: domain=%p tid=%d core=%s", method_name, domain, (int)tid_now(), g_core_dir);

    char dll[600];
    snprintf(dll, sizeof(dll), "%s/Be5Bootstrap.dll", g_core_dir);
    MonoAssembly *asm_ = asm_open(domain, dll);
    if (!asm_) {
        LOGE("bootstrap[%s]: mono_domain_assembly_open failed: %s", method_name, dll);
        return 12;
    }
    MonoImage *img = asm_image(asm_);
    if (!img) {
        LOGE("bootstrap[%s]: assembly image unavailable", method_name);
        return 18;
    }
    MonoClass *klass = class_from_name(img, "Be5Bootstrap", "Boot");
    if (!klass) {
        LOGE("bootstrap[%s]: class Be5Bootstrap.Boot not found", method_name);
        return 13;
    }
    MonoMethod *meth = method_from_name(klass, method_name, 0);
    if (!meth) {
        LOGE("bootstrap[%s]: method %s() not found", method_name, method_name);
        return 14;
    }
    MonoObject *exc = NULL;
    LOGI("bootstrap[%s]: invoking Be5Bootstrap.Boot.%s()", method_name, method_name);
    MonoObject *ret = runtime_invoke(meth, NULL, NULL, &exc);
    if (exc) {
        LOGE("bootstrap[%s]: managed exception", method_name);
        return 16;
    }
    if (!p_object_unbox || !ret) {
        LOGE("bootstrap[%s]: missing return value", method_name);
        return 17;
    }
    void *value = p_object_unbox(ret);
    if (!value) {
        LOGE("bootstrap[%s]: return value unboxing failed", method_name);
        return 19;
    }
    int32_t code = *(int32_t *)value;
    LOGI("bootstrap[%s]: %s() returned code=%d", method_name, method_name, code);
    return code;
}

// Run preloading immediately after domain creation so patchers precede game assembly loading.
static MonoDomain *my_jit_init_version(const char *root_domain_name,
                                       const char *runtime_version,
                                       const char *runtime_dir) {
    resolve_helpers();
    if (!g_real_jit_init_version) {
        if (g_mono)
            g_real_jit_init_version = (mono_jit_init_version_t)dlsym(g_mono, "mono_jit_init_version");
    }
    if (!g_real_jit_init_version) {
        LOGE("my_jit_init_version: no real mono_jit_init_version, calling through g_real_dlsym");
        void *real = g_dlsym ? g_dlsym(RTLD_DEFAULT, "mono_jit_init_version") : NULL;
        g_real_jit_init_version = (mono_jit_init_version_t)real;
    }
    if (!g_real_jit_init_version) {
        LOGE("my_jit_init_version: cannot resolve real mono_jit_init_version; failing early");
        return NULL;
    }

    LOGI("mono_jit_init_version(%s,%s) -> real, then early Preload", root_domain_name ? root_domain_name : "?", runtime_version ? runtime_version : "?");
    MonoDomain *domain = g_real_jit_init_version(root_domain_name, runtime_version, runtime_dir);
    LOGI("mono_jit_init_version: domain=%p (early trigger)", domain);

    if (be5_begin_preload(&g_bootstrap_gate)) {
        int code = call_boot_method("Preload");
        be5_finish_preload(&g_bootstrap_gate, code);
        LOGI("bootstrap[Preload]: attempted=1 succeeded=%d code=%d",
             g_bootstrap_gate.preload_succeeded, code);
    }
    return domain;
}

static int is_chain_trigger_name(const char *n) {
    if (!n) return 0;
    return strcmp(n, "Awake") == 0 || strcmp(n, "Start") == 0 || strcmp(n, "Update") == 0;
}

static MonoObject *invoke_common(MonoMethod *method, void *obj, void **params, MonoObject **exc, int is_array) {
    resolve_helpers();
    const char *mname = p_method_name ? p_method_name(method) : NULL;

    // Wait for a Unity lifecycle callback because Chainloader creates a GameObject.
    if (be5_begin_chain(&g_bootstrap_gate, is_chain_trigger_name(mname))) {
        LOGI("CHAIN TRIGGER on %s tid=%d -> bootstrap", mname, (int)tid_now());
        int code = call_boot_method("Chain");
        be5_finish_chain(&g_bootstrap_gate, code);
        LOGI("bootstrap[Chain]: attempted=1 succeeded=%d code=%d",
             g_bootstrap_gate.chain_succeeded, code);
    }

    if (is_array) {
        return g_real_invoke_array ? g_real_invoke_array(method, obj, (void **)params, exc) : NULL;
    }
    return g_real_invoke ? g_real_invoke(method, obj, params, exc) : NULL;
}

static MonoObject *my_runtime_invoke(MonoMethod *method, void *obj, void **params, MonoObject **exc) {
    return invoke_common(method, obj, params, exc, 0);
}

static MonoObject *my_runtime_invoke_array(MonoMethod *method, void *obj, void *params, MonoObject **exc) {
    return invoke_common(method, obj, (void **)params, exc, 1);
}

static int extract_subdir(AAssetManager *am, const char *data_dir, const char *sub) {
    char prefix[128];
    snprintf(prefix, sizeof(prefix), "BepInEx/%s", sub);
    char dest[128];
    snprintf(dest, sizeof(dest), "BepInEx/%s", sub);
    AAssetDir *dir = AAssetManager_openDir(am, prefix);
    if (!dir) {
        if (strcmp(sub, "plugins") == 0 || strcmp(sub, "patchers") == 0) return 0;
        LOGE("AAssetDir open failed: %s", prefix);
        return -1;
    }
    int n = 0;
    const char *name;
    while ((name = AAssetDir_getNextFileName(dir)) != NULL) {
        char src[512], dst[512];
        snprintf(src, sizeof(src), "%s/%s", prefix, name);
        snprintf(dst, sizeof(dst), "%s/%s/%s", data_dir, dest, name);
        // Configuration and user extensions survive APK updates; core assemblies follow the APK.
        if (be5_copy_asset_file(am, src, dst, strcmp(sub, "core") != 0) < 0) {
            LOGE("asset copy failed: %s -> %s: %s", src, dst, strerror(errno));
            AAssetDir_close(dir);
            return -1;
        }
        n++;
    }
    AAssetDir_close(dir);
    LOGI("assets ready: %d files in %s/%s", n, data_dir, dest);
    return n;
}

static int extract_managed(AAssetManager *am, const char *data_dir, char *out_destdir, size_t out_sz) {
    const char *prefix = "bin/Data/Managed";
    char destdir[512];
    const char *data_dir_name = getenv("BEPINEX_DATA_DIR_NAME");
#ifdef GAME_DATA_DIR
    if (!data_dir_name || !data_dir_name[0]) data_dir_name = GAME_DATA_DIR;
#endif
    if (data_dir_name && data_dir_name[0])
        snprintf(destdir, sizeof(destdir), "%s/%s/Managed", data_dir, data_dir_name);
    else
        snprintf(destdir, sizeof(destdir), "%s/Managed", data_dir);
    if (out_destdir) snprintf(out_destdir, out_sz, "%s", destdir);
    AAssetDir *dir = AAssetManager_openDir(am, prefix);
    if (!dir) {
        LOGE("AAssetDir open failed: %s", prefix);
        return -1;
    }
    int n = 0;
    const char *name;
    while ((name = AAssetDir_getNextFileName(dir)) != NULL) {
        char src[512], dst[512];
        snprintf(src, sizeof(src), "%s/%s", prefix, name);
        snprintf(dst, sizeof(dst), "%s/%s", destdir, name);
        if (be5_copy_asset_file(am, src, dst, 0) < 0) {
            LOGE("managed asset copy failed: %s -> %s: %s", src, dst, strerror(errno));
            AAssetDir_close(dir);
            return -1;
        }
        n++;
    }
    AAssetDir_close(dir);
    if (n == 0) {
        LOGE("no managed assemblies found in %s", prefix);
        return -1;
    }
    LOGI("managed assemblies ready: %d files in %s", n, destdir);
    return n;
}

static jobject get_application(JNIEnv *env) {
    jclass cls = (*env)->FindClass(env, "android/app/ActivityThread");
    if (!cls) {
        LOGE("FindClass ActivityThread failed");
        return NULL;
    }
    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "currentApplication",
                                              "()Landroid/app/Application;");
    if (!mid) {
        LOGE("currentApplication method not found");
        return NULL;
    }
    return (*env)->CallStaticObjectMethod(env, cls, mid);
}

static int extract_assets(JNIEnv *env) {
    jobject app = get_application(env);
    if (!app) {
        LOGE("no application context");
        return -1;
    }
    jclass ctxCls = (*env)->GetObjectClass(env, app);

    jmethodID getExt = (*env)->GetMethodID(env, ctxCls, "getExternalFilesDir",
                                           "(Ljava/lang/String;)Ljava/io/File;");
    jmethodID getAbs = (*env)->GetMethodID(env, (*env)->FindClass(env, "java/io/File"),
                                           "getAbsolutePath", "()Ljava/lang/String;");
    if (!getExt || !getAbs) {
        LOGE("JNI methods not found (getExternalFilesDir/getAbsolutePath)");
        return -1;
    }
    jobject file = (*env)->CallObjectMethod(env, app, getExt, NULL);
    if (!file) {
        LOGE("getExternalFilesDir returned null");
        return -1;
    }
    jstring jpath = (*env)->CallObjectMethod(env, file, getAbs);
    if (!jpath) {
        LOGE("getAbsolutePath returned null");
        return -1;
    }
    const char *data_dir = (*env)->GetStringUTFChars(env, jpath, NULL);
    if (!data_dir) {
        LOGE("GetStringUTFChars failed");
        return -1;
    }
    snprintf(g_data_dir, sizeof(g_data_dir), "%s", data_dir);
    LOGI("external files dir = %s", g_data_dir);

    jmethodID getAssets = (*env)->GetMethodID(env, ctxCls, "getAssets",
                                              "()Landroid/content/res/AssetManager;");
    jobject assets = (*env)->CallObjectMethod(env, app, getAssets);
    AAssetManager *am = assets ? AAssetManager_fromJava(env, assets) : NULL;
    if (!am) {
        LOGE("AAssetManager_fromJava failed");
        (*env)->ReleaseStringUTFChars(env, jpath, data_dir);
        return -1;
    }

    char mgd_path[512];
    if (extract_subdir(am, g_data_dir, "core") <= 0 ||
        extract_subdir(am, g_data_dir, "config") < 0 ||
        extract_subdir(am, g_data_dir, "patchers") < 0 ||
        extract_subdir(am, g_data_dir, "plugins") < 0 ||
        extract_managed(am, g_data_dir, mgd_path, sizeof(mgd_path)) < 0) {
        (*env)->ReleaseStringUTFChars(env, jpath, data_dir);
        return -1;
    }

    setenv("BEPINEX_MANAGED_DIR", mgd_path, 1);
    snprintf(g_core_dir, sizeof(g_core_dir), "%s/BepInEx/core", g_data_dir);
    setenv("BEPINEX_CORE_DIR", g_core_dir, 1);
    setenv("BEPINEX_DATA_DIR", g_data_dir, 1);

    (*env)->ReleaseStringUTFChars(env, jpath, data_dir);
    LOGI("extraction done -> %s/BepInEx (core=%s)", g_data_dir, g_core_dir);
    return 0;
}

JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *vm, void *reserved) {
    (void)reserved;
    JNIEnv *env = NULL;
    if ((*vm)->GetEnv(vm, (void **)&env, JNI_VERSION_1_6) != JNI_OK) {
        LOGE("GetEnv failed");
        return JNI_VERSION_1_6;
    }
    LOGI("JNI_OnLoad (be5hook) tid=%d", (int)tid_now());

    g_dlsym = (dlsym_fn)dlsym(RTLD_DEFAULT, "dlsym");
    g_dlopen = (dlopen_fn)dlsym(RTLD_DEFAULT, "dlopen");
    LOGI("real dlsym=%p dlopen=%p", (void *)g_dlsym, (void *)g_dlopen);

    if (extract_assets(env) != 0) {
        LOGE("asset preparation failed; loader initialization stopped");
        return JNI_ERR;
    }

    // Probe loaded libraries without loading a second copy of the game's runtime.
    void *m = dlopen("libmain.so", RTLD_NOLOAD | RTLD_LAZY);
    void *u = dlopen("libunity.so", RTLD_NOLOAD | RTLD_LAZY);
    void *mo = dlopen("libmonobdwgc-2.0.so", RTLD_NOLOAD | RTLD_LAZY);
    LOGI("loaded at JNI_OnLoad: libmain=%p libunity=%p libmono=%p", m, u, mo);

    if (m) {
        patch_got("libmain.so", "dlopen", (void *)my_dlopen);
        patch_got("libmain.so", "dlsym", (void *)my_dlsym);
    }
    if (u) {
        hook_libunity();
    } else {
        LOGI("libunity not loaded yet; waiting for libmain dlopen");
    }
    if (mo) {
        resolve_helpers();
    }

    return JNI_VERSION_1_6;
}
