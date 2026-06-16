package defpackage;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentSender;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.content.res.Configuration;
import android.net.Uri;
import android.os.Bundle;
import android.os.LocaleList;
import android.os.Process;
import dalvik.system.PathClassLoader;
import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.math.BigInteger;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/* JADX INFO: loaded from: classes.dex */
public final class HuaweiShellBridge {
    private static final String ACTIVITY_SERVICE = "activity";
    private static final String ADB_INSTALL_NEED_CONFIRM_KEY = "adb_install_need_confirm";
    private static final String BUNDLE = "android.os.Bundle";
    private static final String DEFAULT_HDB_SESSION_INSTALLER_PACKAGE = "com.huawei.appmarket.vehicle";
    private static final int DEFAULT_HDB_SESSION_ORIGINATING_UID = -1;
    private static final String DEFAULT_LEGACY_INSTALLER_PACKAGE = "com.huawei.appmarket.vehicle";
    private static final String[] FRAMEWORK_SERVICE_JARS = {"/system/framework/services.jar", "/system/framework/hwServices.jar", "/system/framework/hwPartBasicplatformServices.jar", "/system/framework/hwPartSecurityServices.jar"};
    private static final String HUAWEI_PM_STUB = "com.huawei.android.content.pm.IHwPackageManager$Stub";
    private static final String HW_ADB_MANAGER = "com.android.server.pm.HwAdbManager";
    private static final int HW_PERMISSION_ALLOW = 1;
    private static final int HW_PERMISSION_DENY = 2;
    private static final String HW_PERMISSION_MANAGER = "com.huawei.securitycenter.HwPermissionManager";
    private static final String IAM_STUB = "android.app.IActivityManager$Stub";
    private static final String IPM_STUB = "android.content.pm.IPackageManager$Stub";
    private static final int LEGACY_HW_INSTALL_FLAG_DISABLE_VERIFY = 4;
    private static final int LEGACY_INSTALL_FLAG_HDB = 262144;
    private static final int LEGACY_INSTALL_FLAG_REPLACE_EXISTING = 2;
    private static final long LEGACY_INSTALL_POLL_MS = 500;
    private static final long LEGACY_INSTALL_TIMEOUT_MS = 45000;
    private static final int LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN = Integer.MIN_VALUE;
    private static final String PACKAGE_SERVICE = "package";
    private static final long PERM_TYPE_CALL_FORWARD = 1048576;
    private static final long PERM_TYPE_POPUP_BACKGROUND_WINDOW = 2251799813685248L;
    private static final long PERM_TYPE_REQUEST_INSTALL_PACKAGES = 4294967296L;
    private static final long PERM_TYPE_SEND_MMS = 8192;
    private static final long PERM_TYPE_SHORTCUT = 16777216;
    private static final long PERM_TYPE_SYSTEM_ALERT_WINDOW = 536870912;
    private static final String PRIVHELPER_AUTHORITY = "content://com.codex.privhelper.api";
    private static final String PRIVHELPER_STATUS_OK = "ok";
    private static final String SERVICE_MANAGER = "android.os.ServiceManager";

    private HuaweiShellBridge() {
    }

    /* JADX WARN: Can't fix incorrect switch cases order, some code will duplicate */
    /* JADX WARN: Failed to restore switch over string. Please report as a decompilation issue */
    /* JADX WARN: Removed duplicated region for block: B:78:0x0120  */
    /*
        Code decompiled incorrectly, please refer to instructions dump.
        To view partially-correct add '--show-bad-code' argument
    */
    public static void main(java.lang.String[] r21) throws java.lang.Exception {
        /*
            Method dump skipped, instruction units count: 1306
            To view this dump add '--comments-level debug' option
        */
        throw new UnsupportedOperationException("Method not decompiled: defpackage.HuaweiShellBridge.main(java.lang.String[]):void");
    }

    private static void usage() {
        System.out.println("usage:");
        System.out.println("  HuaweiShellBridge set-hdb-key <key>");
        System.out.println("  HuaweiShellBridge get-adb-install-confirm");
        System.out.println("  HuaweiShellBridge set-adb-install-confirm <0|1>");
        System.out.println("  HuaweiShellBridge get-system-locale");
        System.out.println("  HuaweiShellBridge set-system-locale <localeTag>");
        System.out.println("  HuaweiShellBridge scan-install <apkPath>");
        System.out.println("  HuaweiShellBridge scan-install-user <packageName|- > <apkPath> <userId>");
        System.out.println("  HuaweiShellBridge legacy-session-install-user <packageName|- > <apkPath> <userId> [installerPackage]");
        System.out.println("  HuaweiShellBridge hdb-session-install-user <packageName|- > <apkPath> <userId> <hdbKey> [installerPackage] [originatingUid]");
        System.out.println("  HuaweiShellBridge hdb-session-install-user-multi <packageName|- > <userId> <hdbKey> <apkPath1> [apkPath2 ...]");
        System.out.println("  HuaweiShellBridge legacy-session-probe <packageName|- > <apkPath> <userId> [installerPackage]");
        System.out.println("  HuaweiShellBridge privhelper-ping");
        System.out.println("  HuaweiShellBridge privhelper-session-install-user <packageName|- > <apkPath> <userId>");
        System.out.println("  HuaweiShellBridge privhelper-session-install-user-multi <packageName|- > <userId> <apkPath1> [apkPath2 ...]");
        System.out.println("  HuaweiShellBridge get-scan-install-list");
        System.out.println("  HuaweiShellBridge get-system-whitelist <type>");
        System.out.println("  HuaweiShellBridge get-priv-app-type <packageName>");
        System.out.println("  HuaweiShellBridge get-open-file-result-install <apkPath>");
        System.out.println("  HuaweiShellBridge package-uid <userId> <packageName>");
        System.out.println("  HuaweiShellBridge set-installer-perm <userId> <uid> <packageName> <allow|deny>");
        System.out.println("  HuaweiShellBridge set-installer-perm-package <userId> <packageName> <allow|deny>");
        System.out.println("  HuaweiShellBridge set-hw-perm <userId> <packageName> <permissionName> <allow|deny>");
        System.out.println("  HuaweiShellBridge set-session-permissions-result <sessionId> <allow|deny>");
    }

    private static void requireArgs(String[] strArr, int i) {
        if (strArr.length < i) {
            usage();
            throw new IllegalArgumentException("Not enough arguments.");
        }
    }

    private static boolean parseAllowFlag(String str) {
        String lowerCase = str == null ? "" : str.trim().toLowerCase();
        if ("allow".equals(lowerCase) || "1".equals(lowerCase) || "true".equals(lowerCase)) {
            return true;
        }
        if ("deny".equals(lowerCase) || "0".equals(lowerCase) || "false".equals(lowerCase)) {
            return false;
        }
        throw new IllegalArgumentException("allow flag must be allow or deny");
    }

    private static Object getPackageManagerService() throws Exception {
        return Class.forName(IPM_STUB).getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, Class.forName(SERVICE_MANAGER).getMethod("getService", String.class).invoke(null, PACKAGE_SERVICE));
    }

    private static Object getActivityManagerService() throws Exception {
        return Class.forName(IAM_STUB).getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, Class.forName(SERVICE_MANAGER).getMethod("getService", String.class).invoke(null, ACTIVITY_SERVICE));
    }

    private static Object getHuaweiPmService() throws Exception {
        Object packageManagerService = getPackageManagerService();
        return Class.forName(HUAWEI_PM_STUB).getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, packageManagerService.getClass().getMethod("getHwInnerService", new Class[0]).invoke(packageManagerService, new Object[0]));
    }

    private static Object getPackageInstallerService() throws Exception {
        Object packageManagerService = getPackageManagerService();
        return packageManagerService.getClass().getMethod("getPackageInstaller", new Class[0]).invoke(packageManagerService, new Object[0]);
    }

    private static Configuration getSystemConfiguration() throws Exception {
        Object activityManagerService = getActivityManagerService();
        Object objInvoke = activityManagerService.getClass().getMethod("getConfiguration", new Class[0]).invoke(activityManagerService, new Object[0]);
        if (!(objInvoke instanceof Configuration)) {
            throw new IllegalStateException("ActivityManager returned non-Configuration object: " + String.valueOf(objInvoke));
        }
        return (Configuration) objInvoke;
    }

    private static String getCurrentSystemLocaleTag() throws Exception {
        Configuration systemConfiguration = getSystemConfiguration();
        LocaleList locales = systemConfiguration.getLocales();
        if (locales != null && !locales.isEmpty()) {
            return locales.get(0).toLanguageTag();
        }
        Locale locale = systemConfiguration.locale;
        if (locale == null) {
            locale = Locale.getDefault();
        }
        return locale == null ? "" : locale.toLanguageTag();
    }

    private static void setSystemLocale(String str) throws Exception {
        Locale localeTag = parseLocaleTag(str);
        Configuration configuration = new Configuration(getSystemConfiguration());
        configuration.setLocales(new LocaleList(localeTag));
        configuration.setLocale(localeTag);
        configuration.locale = localeTag;
        trySetUserSetLocale(configuration, true);
        if (tryInvokeUpdatePersistentConfiguration(getActivityManagerService(), configuration)) {
        } else {
            throw new NoSuchMethodException("Unable to find ActivityManager persistent configuration update method");
        }
    }

    private static boolean tryInvokeUpdatePersistentConfiguration(Object obj, Configuration configuration) throws Exception {
        Method[] methods = obj.getClass().getMethods();
        int length = methods.length;
        for (int i = 0; i < length; i += HW_PERMISSION_ALLOW) {
            Method method = methods[i];
            if (method.getName().startsWith("updatePersistentConfiguration")) {
                Class<?>[] parameterTypes = method.getParameterTypes();
                try {
                    if (parameterTypes.length == HW_PERMISSION_ALLOW && Configuration.class.equals(parameterTypes[0])) {
                        method.invoke(obj, configuration);
                        return true;
                    }
                    if (parameterTypes.length == 2 && Configuration.class.equals(parameterTypes[0]) && String.class.equals(parameterTypes[HW_PERMISSION_ALLOW])) {
                        method.invoke(obj, configuration, "com.android.shell");
                        return true;
                    }
                    if (parameterTypes.length == 3 && Configuration.class.equals(parameterTypes[0]) && String.class.equals(parameterTypes[HW_PERMISSION_ALLOW]) && String.class.equals(parameterTypes[2])) {
                        method.invoke(obj, configuration, "com.android.shell", null);
                        return true;
                    }
                } catch (IllegalArgumentException e) {
                }
            }
        }
        return false;
    }

    private static Locale parseLocaleTag(String str) {
        if (str == null) {
            throw new IllegalArgumentException("localeTag cannot be null");
        }
        String strReplace = str.trim().replace('_', '-');
        if (strReplace.isEmpty()) {
            throw new IllegalArgumentException("localeTag cannot be empty");
        }
        Locale localeForLanguageTag = Locale.forLanguageTag(strReplace);
        if (localeForLanguageTag == null || localeForLanguageTag.getLanguage() == null || localeForLanguageTag.getLanguage().trim().isEmpty()) {
            throw new IllegalArgumentException("Invalid localeTag: " + str);
        }
        return localeForLanguageTag;
    }

    private static void trySetUserSetLocale(Configuration configuration, boolean z) {
        try {
            Configuration.class.getField("userSetLocale").setBoolean(configuration, z);
        } catch (Throwable th) {
        }
    }

    private static int resolvePackageUid(String str, int i) throws Exception {
        Object packageManagerService = getPackageManagerService();
        try {
            return requireResolvedUid(str, i, packageManagerService.getClass().getMethod("getPackageUid", String.class, Long.TYPE, Integer.TYPE).invoke(packageManagerService, str, 0L, Integer.valueOf(i)));
        } catch (NoSuchMethodException e) {
            try {
                return requireResolvedUid(str, i, packageManagerService.getClass().getMethod("getPackageUid", String.class, Integer.TYPE, Integer.TYPE).invoke(packageManagerService, str, 0, Integer.valueOf(i)));
            } catch (NoSuchMethodException e2) {
                try {
                    return requireResolvedUid(str, i, packageManagerService.getClass().getMethod("getPackageUid", String.class, Integer.TYPE).invoke(packageManagerService, str, 0));
                } catch (NoSuchMethodException e3) {
                    throw new NoSuchMethodException("Unable to resolve getPackageUid(...) on IPackageManager");
                }
            }
        }
    }

    private static int requireResolvedUid(String str, int i, Object obj) {
        if (!(obj instanceof Integer)) {
            throw new IllegalStateException("PackageManager did not return an integer uid");
        }
        int iIntValue = ((Integer) obj).intValue();
        if (iIntValue < 0) {
            throw new IllegalArgumentException("Package '" + str + "' was not found for user " + i);
        }
        return iIntValue;
    }

    private static void setHdbKey(String str) throws Exception {
        Object huaweiPmService = getHuaweiPmService();
        huaweiPmService.getClass().getMethod("setHdbKey", String.class).invoke(huaweiPmService, str);
    }

    private static String getAdbInstallNeedConfirmSetting() throws Exception {
        return secureSettingReadWrite(ADB_INSTALL_NEED_CONFIRM_KEY, null, 0);
    }

    private static void setAdbInstallNeedConfirmSetting(String str) throws Exception {
        secureSettingReadWrite(ADB_INSTALL_NEED_CONFIRM_KEY, str, HW_PERMISSION_ALLOW);
    }

    private static String secureSettingReadWrite(String str, String str2, int i) throws Exception {
        Method declaredMethod = loadFrameworkServiceClass(HW_ADB_MANAGER).getDeclaredMethod("secureSettingRW", String.class, String.class, Integer.TYPE);
        declaredMethod.setAccessible(true);
        Object objInvoke = declaredMethod.invoke(null, str, str2, Integer.valueOf(i));
        if (objInvoke == null) {
            return null;
        }
        return String.valueOf(objInvoke);
    }

    private static Class<?> loadFrameworkServiceClass(String str) throws Exception {
        try {
            return Class.forName(str);
        } catch (ClassNotFoundException e) {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < FRAMEWORK_SERVICE_JARS.length; i += HW_PERMISSION_ALLOW) {
                if (i > 0) {
                    sb.append(File.pathSeparatorChar);
                }
                sb.append(FRAMEWORK_SERVICE_JARS[i]);
            }
            ClassLoader contextClassLoader = Thread.currentThread().getContextClassLoader();
            if (contextClassLoader == null) {
                contextClassLoader = HuaweiShellBridge.class.getClassLoader();
            }
            if (contextClassLoader == null) {
                contextClassLoader = ClassLoader.getSystemClassLoader();
            }
            return Class.forName(str, false, new PathClassLoader(sb.toString(), contextClassLoader));
        }
    }

    private static boolean scanInstallApk(String str) throws Exception {
        Object huaweiPmService = getHuaweiPmService();
        return Boolean.TRUE.equals(huaweiPmService.getClass().getMethod("scanInstallApk", String.class).invoke(huaweiPmService, str));
    }

    private static boolean scanInstallApkWithUser(String str, String str2, int i) throws Exception {
        Object huaweiPmService = getHuaweiPmService();
        return Boolean.TRUE.equals(huaweiPmService.getClass().getMethod("scanInstallApkWithUser", String.class, String.class, Integer.TYPE).invoke(huaweiPmService, str, str2, Integer.valueOf(i)));
    }

    private static Object getScanInstallList() throws Exception {
        Object huaweiPmService = getHuaweiPmService();
        return huaweiPmService.getClass().getMethod("getScanInstallList", new Class[0]).invoke(huaweiPmService, new Object[0]);
    }

    private static Object getSystemWhiteList(String str) throws Exception {
        Object huaweiPmService = getHuaweiPmService();
        return huaweiPmService.getClass().getMethod("getSystemWhiteList", String.class).invoke(huaweiPmService, str);
    }

    private static int getPrivilegeAppType(String str) throws Exception {
        Object huaweiPmService = getHuaweiPmService();
        return ((Integer) huaweiPmService.getClass().getMethod("getPrivilegeAppType", String.class).invoke(huaweiPmService, str)).intValue();
    }

    private static int getOpenFileResultForInstall(String str) throws Exception {
        Object huaweiPmService = getHuaweiPmService();
        Class<?> cls = Class.forName("java.io.File");
        Object objNewInstance = cls.getConstructor(String.class).newInstance(str);
        Class<?> cls2 = Class.forName("android.net.Uri");
        Object objInvoke = cls2.getMethod("fromFile", cls).invoke(null, objNewInstance);
        Class<?> cls3 = Class.forName("android.content.Intent");
        Object objNewInstance2 = cls3.getConstructor(new Class[0]).newInstance(new Object[0]);
        Method method = cls3.getMethod("setAction", String.class);
        Method method2 = cls3.getMethod("setDataAndType", cls2, String.class);
        Method method3 = cls3.getMethod("addFlags", Integer.TYPE);
        method.invoke(objNewInstance2, "android.intent.action.VIEW");
        method2.invoke(objNewInstance2, objInvoke, "application/vnd.android.package-archive");
        method3.invoke(objNewInstance2, 268435456);
        return ((Integer) huaweiPmService.getClass().getMethod("getOpenFileResult", cls3).invoke(huaweiPmService, objNewInstance2)).intValue();
    }

    private static int legacySessionInstallUser(String str, String str2, int i, String str3, int i2) throws Exception {
        return legacySessionInstallUser(str, new String[]{str2}, i, str3, i2, (String) null);
    }

    private static int legacySessionInstallUser(String str, String str2, int i, String str3, int i2, String str4) throws Exception {
        return legacySessionInstallUser(str, new String[]{str2}, i, str3, i2, str4);
    }

    /* JADX WARN: Removed duplicated region for block: B:124:0x02d8  */
    /* JADX WARN: Removed duplicated region for block: B:136:0x02fd  */
    /* JADX WARN: Removed duplicated region for block: B:142:0x030e A[ADDED_TO_REGION] */
    /* JADX WARN: Removed duplicated region for block: B:164:0x0302 A[EXC_TOP_SPLITTER, SYNTHETIC] */
    /* JADX WARN: Removed duplicated region for block: B:192:0x0319 A[EXC_TOP_SPLITTER, SYNTHETIC] */
    /* JADX WARN: Removed duplicated region for block: B:194:? A[SYNTHETIC] */
    /*
        Code decompiled incorrectly, please refer to instructions dump.
        To view partially-correct add '--show-bad-code' argument
    */
    private static int legacySessionInstallUser(java.lang.String r21, java.lang.String[] r22, int r23, java.lang.String r24, int r25, java.lang.String r26) throws java.lang.Exception {
        /*
            Method dump skipped, instruction units count: 809
            To view this dump add '--comments-level debug' option
        */
        throw new UnsupportedOperationException("Method not decompiled: defpackage.HuaweiShellBridge.legacySessionInstallUser(java.lang.String, java.lang.String[], int, java.lang.String, int, java.lang.String):int");
    }

    private static Bundle privilegedHelperCapabilities() throws Exception {
        return callPrivilegedHelper("capabilities", new Bundle());
    }

    private static int privilegedHelperSessionInstallUser(String str, String[] strArr, int i) throws Exception {
        if (strArr == null || strArr.length == 0) {
            throw new IllegalArgumentException("APK path is required for privileged helper install.");
        }
        Bundle bundle = new Bundle();
        if (str != null && !str.trim().isEmpty()) {
            bundle.putString("packageName", str.trim());
        }
        bundle.putInt("targetUserId", i);
        bundle.putBoolean("acceptPermissions", true);
        if (strArr.length == HW_PERMISSION_ALLOW) {
            bundle.putString("apkPath", strArr[0]);
            return requirePrivilegedHelperInstallSuccess(callPrivilegedHelper("session_install_single", bundle), str, i);
        }
        bundle.putStringArray("apkPaths", strArr);
        return requirePrivilegedHelperInstallSuccess(callPrivilegedHelper("session_install_multi", bundle), str, i);
    }

    private static Bundle callPrivilegedHelper(String str, Bundle bundle) throws Exception {
        Bundle bundleCall = resolveSystemContext().getContentResolver().call(Uri.parse(PRIVHELPER_AUTHORITY), str, (String) null, bundle);
        if (bundleCall == null) {
            throw new IllegalStateException("Privileged helper returned null for method " + str);
        }
        if (!PRIVHELPER_STATUS_OK.equalsIgnoreCase(bundleCall.getString("status", ""))) {
            throw new IllegalStateException("Privileged helper call failed method=" + str + " error=" + bundleCall.getString("error", "unknown") + " exception=" + bundleCall.getString("exception", ""));
        }
        return bundleCall;
    }

    private static int requirePrivilegedHelperInstallSuccess(Bundle bundle, String str, int i) {
        int i2 = bundle.getInt("statusCode", LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN);
        String string = bundle.getString("statusMessage", "");
        int i3 = bundle.getInt("sessionId", DEFAULT_HDB_SESSION_ORIGINATING_UID);
        int i4 = bundle.getInt("installExistingResultCode", LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN);
        legacyTrace("privhelper.result", "sessionId=" + i3 + " statusCode=" + i2 + " statusMessage=" + String.valueOf(string) + " installExistingResultCode=" + i4 + " packageName=" + String.valueOf(str) + " userId=" + i);
        if (i2 != 0) {
            throw new IllegalStateException("Privileged helper install failed: " + String.valueOf(string));
        }
        return i3;
    }

    private static void legacySessionProbe(String str, String str2, int i, String str3, int i2) throws Exception {
        String strTrim;
        String strResolvePackageNameFromArchive;
        long j;
        boolean z;
        String str4;
        PackageInstaller.Session sessionOpenSession = null;
        if (str3 == null) {
            strTrim = null;
        } else {
            strTrim = str3.trim().isEmpty() ? "com.huawei.appmarket.vehicle" : str3.trim();
        }
        legacyTrace("probe.begin", "userId=" + i + " installer=" + String.valueOf(strTrim) + " originatingUid=" + i2 + " apkPath=" + str2);
        Context contextResolveSystemContext = resolveSystemContext();
        legacyTrace("probe.systemContext", "package=" + contextResolveSystemContext.getPackageName());
        if (str == null || str.trim().isEmpty()) {
            strResolvePackageNameFromArchive = resolvePackageNameFromArchive(contextResolveSystemContext, str2);
        } else {
            strResolvePackageNameFromArchive = str.trim();
        }
        legacyTrace("probe.effectivePackage", String.valueOf(strResolvePackageNameFromArchive));
        File file = new File(str2);
        long jResolvePackageLastUpdateTime = -1;
        legacyTrace("probe.apk", "exists=" + file.isFile() + " size=" + (file.isFile() ? file.length() : -1L));
        if (!file.isFile()) {
            throw new IllegalArgumentException("APK file was not found: " + str2);
        }
        if (strResolvePackageNameFromArchive != null && !strResolvePackageNameFromArchive.isEmpty()) {
            jResolvePackageLastUpdateTime = resolvePackageLastUpdateTime(strResolvePackageNameFromArchive, i);
        }
        if (jResolvePackageLastUpdateTime > 0) {
            j = jResolvePackageLastUpdateTime;
            z = true;
        } else {
            j = jResolvePackageLastUpdateTime;
            z = false;
        }
        legacyTrace("probe.packageState", "previousUpdateTime=" + j + " isUpdate=" + z);
        PackageInstaller packageInstallerCreateLegacyPackageInstaller = createLegacyPackageInstaller(i, strTrim);
        legacyTrace("probe.packageInstaller", packageInstallerCreateLegacyPackageInstaller.getClass().getName());
        if (strResolvePackageNameFromArchive == null || strResolvePackageNameFromArchive.isEmpty()) {
            str4 = "unknown.package";
        } else {
            str4 = strResolvePackageNameFromArchive;
        }
        PackageInstaller.SessionParams sessionParamsBuildLegacySessionParams = buildLegacySessionParams(contextResolveSystemContext, str4, new File[]{file}, z, i2, null);
        legacyTrace("probe.sessionParams", describeLegacySessionParams(sessionParamsBuildLegacySessionParams));
        int iCreateSession = packageInstallerCreateLegacyPackageInstaller.createSession(sessionParamsBuildLegacySessionParams);
        legacyTrace("probe.createSession", "sessionId=" + iCreateSession);
        try {
            sessionOpenSession = packageInstallerCreateLegacyPackageInstaller.openSession(iCreateSession);
            legacyTrace("probe.openSession", String.valueOf(sessionOpenSession));
            if (sessionOpenSession != null) {
                try {
                    sessionOpenSession.abandon();
                    legacyTrace("probe.abandon", "sessionId=" + iCreateSession);
                } catch (Throwable th) {
                    try {
                        sessionOpenSession.close();
                    } catch (Throwable th2) {
                    }
                }
            }
        } finally {
        }
    }

    private static Context resolveSystemContext() throws Exception {
        try {
            Object objInvoke = Class.forName("android.app.AppGlobals").getMethod("getInitialApplication", new Class[0]).invoke(null, new Object[0]);
            if (objInvoke instanceof Context) {
                return (Context) objInvoke;
            }
        } catch (Throwable th) {
        }
        Class<?> cls = Class.forName("android.app.ActivityThread");
        Object objInvoke2 = cls.getMethod("currentActivityThread", new Class[0]).invoke(null, new Object[0]);
        if (objInvoke2 == null) {
            Method declaredMethod = cls.getDeclaredMethod("systemMain", new Class[0]);
            declaredMethod.setAccessible(true);
            objInvoke2 = declaredMethod.invoke(null, new Object[0]);
        }
        Object objInvoke3 = cls.getMethod("getSystemContext", new Class[0]).invoke(objInvoke2, new Object[0]);
        if (!(objInvoke3 instanceof Context)) {
            throw new IllegalStateException("Unable to resolve a system context for legacy session install.");
        }
        return (Context) objInvoke3;
    }

    private static String resolvePackageNameFromArchive(Context context, String str) {
        try {
            PackageInfo packageArchiveInfo = context.getPackageManager().getPackageArchiveInfo(str, 0);
            if (packageArchiveInfo == null) {
                return null;
            }
            return packageArchiveInfo.packageName;
        } catch (Throwable th) {
            return null;
        }
    }

    private static PackageInstaller createLegacyPackageInstaller(int i, String str) throws Exception {
        Object packageInstallerService = getPackageInstallerService();
        Constructor declaredConstructor = PackageInstaller.class.getDeclaredConstructor(Class.forName("android.content.pm.IPackageInstaller"), String.class, Integer.TYPE);
        declaredConstructor.setAccessible(true);
        return (PackageInstaller) declaredConstructor.newInstance(packageInstallerService, str, Integer.valueOf(i));
    }

    private static PackageInstaller.SessionParams buildLegacySessionParams(Context context, String str, File[] fileArr, boolean z, int i, String str2) throws Exception {
        PackageInstaller.SessionParams sessionParams = new PackageInstaller.SessionParams(HW_PERMISSION_ALLOW);
        sessionParams.setAppPackageName(str);
        sessionParams.setSize(sumApkFileLengths(fileArr));
        applyOriginatingUid(sessionParams, i);
        applyLegacyInstallFlags(sessionParams, z);
        applyLegacyHdbFields(sessionParams, fileArr, str2);
        applyLegacyHwInstallFlags(context, sessionParams);
        return sessionParams;
    }

    private static String describeLegacySessionParams(PackageInstaller.SessionParams sessionParams) {
        return "mode=" + readIntField(sessionParams, "mode") + " installFlags=" + readIntField(sessionParams, "installFlags") + " hwInstallFlags=" + readIntField(sessionParams, "hwInstallFlags") + " originatingUid=" + readIntField(sessionParams, "originatingUid") + " hdbArgIndex=" + readIntField(sessionParams, "hdbArgIndex") + " hdbEncode=" + readStringField(sessionParams, "hdbEncode") + " hdbArgs=" + readStringArrayField(sessionParams, "hdbArgs");
    }

    private static String readIntField(Object obj, String str) {
        try {
            Field declaredField = obj.getClass().getDeclaredField(str);
            declaredField.setAccessible(true);
            return String.valueOf(declaredField.getInt(obj));
        } catch (Throwable th) {
            return "n/a";
        }
    }

    private static String readStringField(Object obj, String str) {
        try {
            Field declaredField = obj.getClass().getDeclaredField(str);
            declaredField.setAccessible(true);
            return String.valueOf(declaredField.get(obj));
        } catch (Throwable th) {
            return "n/a";
        }
    }

    private static String readStringArrayField(Object obj, String str) {
        try {
            Field declaredField = obj.getClass().getDeclaredField(str);
            declaredField.setAccessible(true);
            Object obj2 = declaredField.get(obj);
            if (!(obj2 instanceof String[])) {
                return String.valueOf(obj2);
            }
            String[] strArr = (String[]) obj2;
            StringBuilder sb = new StringBuilder("[");
            for (int i = 0; i < strArr.length; i += HW_PERMISSION_ALLOW) {
                if (i > 0) {
                    sb.append(", ");
                }
                sb.append(strArr[i]);
            }
            sb.append(']');
            return sb.toString();
        } catch (Throwable th) {
            return "n/a";
        }
    }

    private static void applyOriginatingUid(PackageInstaller.SessionParams sessionParams, int i) {
        try {
            Field declaredField = PackageInstaller.SessionParams.class.getDeclaredField("originatingUid");
            declaredField.setAccessible(true);
            declaredField.setInt(sessionParams, i);
        } catch (Throwable th) {
        }
    }

    private static String parseInstallerPackageArg(String str) {
        if (str == null) {
            return null;
        }
        String strTrim = str.trim();
        if (strTrim.isEmpty()) {
            return "";
        }
        if ("null".equalsIgnoreCase(strTrim) || "-".equals(strTrim)) {
            return null;
        }
        return strTrim;
    }

    private static boolean looksLikeApkPathArg(String str) {
        if (str == null) {
            return false;
        }
        String strTrim = str.trim();
        if (strTrim.isEmpty()) {
            return false;
        }
        return strTrim.startsWith("/") || strTrim.startsWith("./") || strTrim.startsWith("../") || strTrim.endsWith(".apk") || strTrim.endsWith(".apks") || strTrim.endsWith(".xapk");
    }

    private static void applyLegacyInstallFlags(PackageInstaller.SessionParams sessionParams, boolean z) {
        if (!z) {
            return;
        }
        try {
            Field declaredField = PackageInstaller.SessionParams.class.getDeclaredField("installFlags");
            declaredField.setAccessible(true);
            declaredField.setInt(sessionParams, declaredField.getInt(sessionParams) | 2);
        } catch (Throwable th) {
        }
    }

    private static void applyLegacyHdbFields(PackageInstaller.SessionParams sessionParams, File[] fileArr, String str) {
        if (str == null || str.trim().isEmpty()) {
            return;
        }
        String strTrim = str.trim();
        String[] strArr = new String[fileArr.length];
        for (int i = 0; i < fileArr.length; i += HW_PERMISSION_ALLOW) {
            strArr[i] = fileArr[i].getAbsolutePath();
        }
        writeField(sessionParams, "hdbEncode", computeHdbSessionEncode(strTrim, strArr, 0));
        writeField(sessionParams, "hdbArgIndex", 0);
        writeField(sessionParams, "hdbArgs", strArr);
        orIntField(sessionParams, "installFlags", LEGACY_INSTALL_FLAG_HDB);
    }

    private static void applyLegacyHwInstallFlags(Context context, PackageInstaller.SessionParams sessionParams) {
        int iResolveLegacyHwInstallFlags = resolveLegacyHwInstallFlags(context);
        if (iResolveLegacyHwInstallFlags == 0 || trySetHwInstallFlagsViaApi("com.huawei.android.app.PackageManagerEx", sessionParams, iResolveLegacyHwInstallFlags) || trySetHwInstallFlagsViaApi("com.hihonor.android.app.PackageManagerEx", sessionParams, iResolveLegacyHwInstallFlags)) {
            return;
        }
        try {
            Field declaredField = PackageInstaller.SessionParams.class.getDeclaredField("hwInstallFlags");
            declaredField.setAccessible(true);
            declaredField.setInt(sessionParams, iResolveLegacyHwInstallFlags);
        } catch (Throwable th) {
        }
    }

    private static int resolveLegacyHwInstallFlags(Context context) {
        if (context == null) {
            return 0;
        }
        if (context.checkPermission("com.huawei.permission.INSTALL_APP_DISABLE_VERIFY", Process.myPid(), Process.myUid()) != 0) {
            return 0;
        }
        return LEGACY_HW_INSTALL_FLAG_DISABLE_VERIFY;
    }

    private static boolean trySetHwInstallFlagsViaApi(String str, PackageInstaller.SessionParams sessionParams, int i) {
        try {
            Class.forName(str).getMethod("setHwInstallFlags", PackageInstaller.SessionParams.class, Integer.TYPE).invoke(null, sessionParams, Integer.valueOf(i));
            return true;
        } catch (Throwable th) {
            return false;
        }
    }

    private static String computeHdbSessionEncode(String str, String[] strArr, int i) {
        try {
            MessageDigest messageDigest = MessageDigest.getInstance("SHA-256");
            StringBuilder sbAppend = new StringBuilder(str).append('=');
            for (int i2 = i; i2 < strArr.length; i2 += HW_PERMISSION_ALLOW) {
                if (i2 > i) {
                    sbAppend.append(' ');
                }
                sbAppend.append(strArr[i2]);
            }
            byte[] bArrDigest = messageDigest.digest(sbAppend.toString().getBytes());
            return String.format("%0" + (bArrDigest.length << HW_PERMISSION_ALLOW) + "x", new BigInteger(HW_PERMISSION_ALLOW, bArrDigest));
        } catch (Exception e) {
            throw new IllegalStateException("Unable to compute HDB session hash", e);
        }
    }

    private static void writeField(Object obj, String str, Object obj2) {
        try {
            Field declaredField = obj.getClass().getDeclaredField(str);
            declaredField.setAccessible(true);
            declaredField.set(obj, obj2);
        } catch (Throwable th) {
            throw new IllegalStateException("Unable to write field " + str, th);
        }
    }

    private static void orIntField(Object obj, String str, int i) {
        try {
            Field declaredField = obj.getClass().getDeclaredField(str);
            declaredField.setAccessible(true);
            declaredField.setInt(obj, i | declaredField.getInt(obj));
        } catch (Throwable th) {
            throw new IllegalStateException("Unable to update field " + str, th);
        }
    }

    private static void tryInvokeBooleanSetter(Object obj, String str, boolean z) {
        try {
            obj.getClass().getMethod(str, Boolean.TYPE).invoke(obj, Boolean.valueOf(z));
        } catch (Throwable th) {
        }
    }

    private static void writeApksToSession(PackageInstaller.Session session, File[] fileArr) throws Exception {
        for (int i = 0; i < fileArr.length; i += HW_PERMISSION_ALLOW) {
            writeSingleApkToSession(session, fileArr[i], buildSessionEntryName(fileArr[i], i, fileArr.length));
        }
    }

    private static void writeSingleApkToSession(PackageInstaller.Session session, File file, String str) throws Exception {
        Throwable th;
        FileInputStream fileInputStream;
        OutputStream outputStreamOpenWrite = null;
        try {
            fileInputStream = new FileInputStream(file);
            try {
                outputStreamOpenWrite = session.openWrite(str, 0L, file.length());
                byte[] bArr = new byte[65536];
                while (true) {
                    int i = fileInputStream.read(bArr);
                    if (i == DEFAULT_HDB_SESSION_ORIGINATING_UID) {
                        break;
                    } else {
                        outputStreamOpenWrite.write(bArr, 0, i);
                    }
                }
                session.fsync(outputStreamOpenWrite);
                if (outputStreamOpenWrite != null) {
                    try {
                        outputStreamOpenWrite.close();
                    } catch (Throwable th2) {
                    }
                }
                try {
                    fileInputStream.close();
                } catch (Throwable th3) {
                }
            } catch (Throwable th4) {
                th = th4;
                if (outputStreamOpenWrite != null) {
                    try {
                        outputStreamOpenWrite.close();
                    } catch (Throwable th5) {
                    }
                }
                if (fileInputStream == null) {
                    throw th;
                }
                try {
                    fileInputStream.close();
                    throw th;
                } catch (Throwable th6) {
                    throw th;
                }
            }
        } catch (Throwable th7) {
            th = th7;
            fileInputStream = null;
        }
    }

    private static File[] resolveApkFiles(String[] strArr) {
        if (strArr == null || strArr.length == 0) {
            throw new IllegalArgumentException("At least one APK path is required.");
        }
        File[] fileArr = new File[strArr.length];
        for (int i = 0; i < strArr.length; i += HW_PERMISSION_ALLOW) {
            String str = strArr[i];
            if (str == null || str.trim().isEmpty()) {
                throw new IllegalArgumentException("APK path " + i + " is empty.");
            }
            File file = new File(str);
            if (!file.isFile()) {
                throw new IllegalArgumentException("APK file was not found: " + str);
            }
            fileArr[i] = file;
        }
        return fileArr;
    }

    private static long sumApkFileLengths(File[] fileArr) {
        int length = fileArr.length;
        long length2 = 0;
        for (int i = 0; i < length; i += HW_PERMISSION_ALLOW) {
            length2 += fileArr[i].length();
        }
        return length2;
    }

    private static String describeApkFiles(File[] fileArr) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < fileArr.length; i += HW_PERMISSION_ALLOW) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(fileArr[i].getAbsolutePath()).append(" (").append(fileArr[i].length()).append(")");
        }
        sb.append(']');
        return sb.toString();
    }

    private static String describeSessionEntryNames(File[] fileArr) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < fileArr.length; i += HW_PERMISSION_ALLOW) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(buildSessionEntryName(fileArr[i], i, fileArr.length));
        }
        sb.append(']');
        return sb.toString();
    }

    private static String buildSessionEntryName(File file, int i, int i2) {
        if (i2 == HW_PERMISSION_ALLOW) {
            return "base.apk";
        }
        String name = file.getName();
        if (name == null || name.trim().isEmpty()) {
            name = "split_" + i + ".apk";
        }
        String strReplaceAll = name.replaceAll("[^A-Za-z0-9._-]", "_");
        if (!strReplaceAll.toLowerCase().endsWith(".apk")) {
            return strReplaceAll + ".apk";
        }
        return strReplaceAll;
    }

    private static Object createShellLocalIntentReceiver() throws Exception {
        Constructor<?>[] declaredConstructors = loadHiddenSystemClass("com.android.server.pm.PackageManagerShellCommand$LocalIntentReceiver").getDeclaredConstructors();
        int length = declaredConstructors.length;
        for (int i = 0; i < length; i += HW_PERMISSION_ALLOW) {
            Constructor<?> constructor = declaredConstructors[i];
            constructor.setAccessible(true);
            Class<?>[] parameterTypes = constructor.getParameterTypes();
            if (parameterTypes.length != 0) {
                if (parameterTypes.length == HW_PERMISSION_ALLOW) {
                    return constructor.newInstance(null);
                }
            } else {
                return constructor.newInstance(new Object[0]);
            }
        }
        throw new IllegalStateException("No usable LocalIntentReceiver constructor found");
    }

    private static IntentSender getShellLocalIntentSender(Object obj) throws Exception {
        return (IntentSender) obj.getClass().getMethod("getIntentSender", new Class[0]).invoke(obj, new Object[0]);
    }

    private static Intent getShellLocalIntentResult(Object obj) throws Exception {
        return (Intent) obj.getClass().getMethod("getResult", new Class[0]).invoke(obj, new Object[0]);
    }

    private static Class<?> loadHiddenSystemClass(String str) throws Exception {
        try {
            return Class.forName(str);
        } catch (ClassNotFoundException e) {
            String[] strArr = {"/system/framework/services.jar", "/system/framework/hwServices.jar", "/system/framework/hwPartBasicplatformServices.jar", "/system/framework/hwPartSecurityServices.jar"};
            Constructor<?> constructor = Class.forName("dalvik.system.PathClassLoader").getConstructor(String.class, ClassLoader.class);
            ClassLoader classLoader = HuaweiShellBridge.class.getClassLoader();
            for (int i = 0; i < LEGACY_HW_INSTALL_FLAG_DISABLE_VERIFY; i += HW_PERMISSION_ALLOW) {
                try {
                    return Class.forName(str, true, (ClassLoader) constructor.newInstance(strArr[i], classLoader));
                } catch (Throwable th) {
                }
            }
            throw new ClassNotFoundException(str);
        }
    }

    private static Thread startSessionApprovalLoop(final int i, final AtomicBoolean atomicBoolean) {
        Thread thread = new Thread(new Runnable() { // from class: HuaweiShellBridge.1
            @Override // java.lang.Runnable
            public void run() {
                boolean z = false;
                int i2 = 0;
                while (!atomicBoolean.get()) {
                    try {
                        HuaweiShellBridge.setSessionPermissionsResult(i, true);
                        if (!z) {
                            HuaweiShellBridge.legacyTrace("approval.result", "sessionId=" + i + " accepted=true");
                            z = true;
                        }
                    } catch (Throwable th) {
                        if (i2 < 5) {
                            HuaweiShellBridge.legacyTrace("approval.error", "sessionId=" + i + " error=" + th.getClass().getName() + ": " + String.valueOf(th.getMessage()));
                            i2 += HuaweiShellBridge.HW_PERMISSION_ALLOW;
                        }
                    }
                    try {
                        Thread.sleep(50L);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                }
            }
        }, "codex-session-approval-" + i);
        thread.setDaemon(true);
        thread.start();
        return thread;
    }

    private static void pumpSessionApproval(int i, long j) {
        long jCurrentTimeMillis = System.currentTimeMillis() + Math.max(0L, j);
        boolean z = false;
        int i2 = 0;
        while (System.currentTimeMillis() < jCurrentTimeMillis) {
            try {
                setSessionPermissionsResult(i, true);
                if (!z) {
                    legacyTrace("approval.pump.result", "sessionId=" + i + " accepted=true");
                    z = true;
                }
            } catch (Throwable th) {
                if (i2 < 5) {
                    legacyTrace("approval.pump.error", "sessionId=" + i + " error=" + th.getClass().getName() + ": " + String.valueOf(th.getMessage()));
                    i2 += HW_PERMISSION_ALLOW;
                }
            }
            try {
                Thread.sleep(10L);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    private static void primeSessionApproval(int i, long j) {
        long jCurrentTimeMillis = System.currentTimeMillis() + Math.max(0L, j);
        boolean z = false;
        int i2 = 0;
        while (System.currentTimeMillis() < jCurrentTimeMillis) {
            try {
                setSessionPermissionsResult(i, true);
                if (!z) {
                    legacyTrace("approval.prime.result", "sessionId=" + i + " accepted=true");
                    z = true;
                }
            } catch (Throwable th) {
                if (i2 < 5) {
                    legacyTrace("approval.prime.error", "sessionId=" + i + " error=" + th.getClass().getName() + ": " + String.valueOf(th.getMessage()));
                    i2 += HW_PERMISSION_ALLOW;
                }
            }
            try {
                Thread.sleep(20L);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    private static String describeCommitResult(Intent intent) {
        if (intent == null) {
            return "null";
        }
        return "status=" + intent.getIntExtra("android.content.pm.extra.STATUS", LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN) + " message=" + String.valueOf(intent.getStringExtra("android.content.pm.extra.STATUS_MESSAGE"));
    }

    private static void waitForLegacyInstallCompletion(PackageInstaller packageInstaller, int i, String str, int i2, long j, Intent intent) throws Exception {
        int intExtra;
        String strValueOf;
        long jCurrentTimeMillis = System.currentTimeMillis() + LEGACY_INSTALL_TIMEOUT_MS;
        if (intent != null) {
            intExtra = intent.getIntExtra("android.content.pm.extra.STATUS", LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN);
        } else {
            intExtra = LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN;
        }
        if (intent != null) {
            strValueOf = String.valueOf(intent.getStringExtra("android.content.pm.extra.STATUS_MESSAGE"));
        } else {
            strValueOf = "null";
        }
        if (intExtra == 0 && hasPackageUpdateCompleted(str, i2, j)) {
            return;
        }
        String strValueOf2 = "";
        while (System.currentTimeMillis() < jCurrentTimeMillis) {
            if (hasPackageUpdateCompleted(str, i2, j)) {
                return;
            }
            try {
                strValueOf2 = String.valueOf(packageInstaller.getSessionInfo(i));
            } catch (Throwable th) {
            }
            if (intExtra != LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN && intExtra != DEFAULT_HDB_SESSION_ORIGINATING_UID && intExtra != 0) {
                throw new IllegalStateException("Legacy session install failed status=" + intExtra + " message=" + strValueOf);
            }
            Thread.sleep(LEGACY_INSTALL_POLL_MS);
        }
        if (hasPackageUpdateCompleted(str, i2, j)) {
            return;
        }
        if (intExtra != LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN && intExtra != DEFAULT_HDB_SESSION_ORIGINATING_UID && intExtra != 0) {
            throw new IllegalStateException("Legacy session install failed status=" + intExtra + " message=" + strValueOf);
        }
        throw new IllegalStateException("Legacy session install timed out for package '" + str + "' sessionId=" + i + " lastCallbackStatus=" + intExtra + " lastCallbackMessage=" + strValueOf + " sessionInfo=" + strValueOf2);
    }

    private static boolean hasPackageUpdateCompleted(String str, int i, long j) {
        try {
            long jResolvePackageLastUpdateTime = resolvePackageLastUpdateTime(str, i);
            if (jResolvePackageLastUpdateTime < 0) {
                return false;
            }
            return j < 0 || jResolvePackageLastUpdateTime > j;
        } catch (Throwable th) {
            return false;
        }
    }

    private static long resolvePackageLastUpdateTime(String str, int i) throws Exception {
        Object packageInfoForUser = getPackageInfoForUser(str, i);
        if (packageInfoForUser == null) {
            return -1L;
        }
        return packageInfoForUser.getClass().getField("lastUpdateTime").getLong(packageInfoForUser);
    }

    private static Object getPackageInfoForUser(String str, int i) throws Exception {
        Object packageManagerService = getPackageManagerService();
        try {
            return packageManagerService.getClass().getMethod("getPackageInfo", String.class, Long.TYPE, Integer.TYPE).invoke(packageManagerService, str, 0L, Integer.valueOf(i));
        } catch (NoSuchMethodException e) {
            try {
                return packageManagerService.getClass().getMethod("getPackageInfo", String.class, Integer.TYPE, Integer.TYPE).invoke(packageManagerService, str, 0, Integer.valueOf(i));
            } catch (NoSuchMethodException e2) {
                try {
                    return packageManagerService.getClass().getMethod("getPackageInfo", String.class, Integer.TYPE).invoke(packageManagerService, str, 0);
                } catch (NoSuchMethodException e3) {
                    return null;
                }
            }
        }
    }

    /* JADX INFO: Access modifiers changed from: private */
    public static void legacyTrace(String str, String str2) {
        System.out.println("legacyTrace step=" + str + " detail=" + str2);
        System.out.flush();
    }

    private static void setInstallerPermission(int i, int i2, String str, boolean z) throws Exception {
        Class<?> cls = Class.forName(HW_PERMISSION_MANAGER);
        Object objInvoke = cls.getMethod("getInstance", new Class[0]).invoke(null, new Object[0]);
        Class<?> cls2 = Class.forName(BUNDLE);
        Object objNewInstance = cls2.getConstructor(new Class[0]).newInstance(new Object[0]);
        Method method = cls2.getMethod("putString", String.class, String.class);
        Method method2 = cls2.getMethod("putInt", String.class, Integer.TYPE);
        Method method3 = cls2.getMethod("putSerializable", String.class, Class.forName("java.io.Serializable"));
        method.invoke(objNewInstance, "name_key", "setHwPermissionForInstaller");
        method2.invoke(objNewInstance, "uid", Integer.valueOf(i2));
        method.invoke(objNewInstance, "packageName", str);
        HashMap map = new HashMap();
        map.put("REQUEST_INSTALL_PACKAGES", Integer.valueOf(z ? 0 : 2));
        method3.invoke(objNewInstance, "grant_permission", map);
        cls.getMethod("setHwPermissionInfo", Integer.TYPE, cls2).invoke(objInvoke, Integer.valueOf(i), objNewInstance);
    }

    private static void setHwPermission(int i, String str, String str2, boolean z) throws Exception {
        Class<?> cls = Class.forName(HW_PERMISSION_MANAGER);
        Object objInvoke = cls.getMethod("getInstance", new Class[0]).invoke(null, new Object[0]);
        Class<?> cls2 = Class.forName(BUNDLE);
        Object objNewInstance = cls2.getConstructor(new Class[0]).newInstance(new Object[0]);
        Method method = cls2.getMethod("putString", String.class, String.class);
        Method method2 = cls2.getMethod("putInt", String.class, Integer.TYPE);
        Method method3 = cls2.getMethod("putLong", String.class, Long.TYPE);
        method.invoke(objNewInstance, "name_key", "setHwPermission");
        method.invoke(objNewInstance, "packageName", str);
        method3.invoke(objNewInstance, "perm_type_key", Long.valueOf(resolveHwPermissionType(str2)));
        method2.invoke(objNewInstance, "operation_key", Integer.valueOf(z ? HW_PERMISSION_ALLOW : 2));
        cls.getMethod("setHwPermissionInfo", Integer.TYPE, cls2).invoke(objInvoke, Integer.valueOf(i), objNewInstance);
    }

    /* JADX INFO: Access modifiers changed from: private */
    public static void setSessionPermissionsResult(int i, boolean z) throws Exception {
        Object packageInstallerService = getPackageInstallerService();
        packageInstallerService.getClass().getMethod("setPermissionsResult", Integer.TYPE, Boolean.TYPE).invoke(packageInstallerService, Integer.valueOf(i), Boolean.valueOf(z));
    }

    /* JADX WARN: Can't fix incorrect switch cases order, some code will duplicate */
    /* JADX WARN: Removed duplicated region for block: B:48:0x00a1  */
    /*
        Code decompiled incorrectly, please refer to instructions dump.
        To view partially-correct add '--show-bad-code' argument
    */
    private static long resolveHwPermissionType(java.lang.String r3) {
        /*
            Method dump skipped, instruction units count: 314
            To view this dump add '--comments-level debug' option
        */
        throw new UnsupportedOperationException("Method not decompiled: defpackage.HuaweiShellBridge.resolveHwPermissionType(java.lang.String):long");
    }

    private static final class LegacyInstallWatcher {
        private final CountDownLatch latch = new CountDownLatch(HuaweiShellBridge.HW_PERMISSION_ALLOW);
        private final AtomicInteger status = new AtomicInteger(HuaweiShellBridge.LEGACY_PACKAGEINSTALLER_STATUS_UNKNOWN);
        private final AtomicReference<String> message = new AtomicReference<>("");
        private final BroadcastReceiver receiver = new BroadcastReceiver() { // from class: HuaweiShellBridge.LegacyInstallWatcher.1
            @Override // android.content.BroadcastReceiver
            public void onReceive(Context context, Intent intent) {
                if (intent == null) {
                    return;
                }
                int intExtra = intent.getIntExtra("android.content.pm.extra.STATUS", HuaweiShellBridge.HW_PERMISSION_ALLOW);
                LegacyInstallWatcher.this.status.set(intExtra);
                String stringExtra = intent.getStringExtra("android.content.pm.extra.STATUS_MESSAGE");
                if (stringExtra == null) {
                    stringExtra = "";
                }
                LegacyInstallWatcher.this.message.set(stringExtra);
                System.out.println("legacySessionInstallUser callback status=" + intExtra + " message=" + stringExtra);
                if (intExtra != HuaweiShellBridge.DEFAULT_HDB_SESSION_ORIGINATING_UID) {
                    LegacyInstallWatcher.this.latch.countDown();
                }
            }
        };

        private LegacyInstallWatcher() {
        }
    }
}
