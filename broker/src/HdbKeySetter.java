package defpackage;

/* JADX INFO: loaded from: classes.dex */
public final class HdbKeySetter {
    private HdbKeySetter() {
    }

    public static void main(String[] strArr) throws Exception {
        if (strArr.length != 1 || strArr[0] == null || strArr[0].isEmpty()) {
            System.err.println("usage: HdbKeySetter <key>");
            System.exit(2);
        }
        String str = strArr[0];
        Object objInvoke = Class.forName("android.content.pm.IPackageManager$Stub").getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, Class.forName("android.os.ServiceManager").getMethod("getService", String.class).invoke(null, "package"));
        Object objInvoke2 = Class.forName("com.huawei.android.content.pm.IHwPackageManager$Stub").getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, objInvoke.getClass().getMethod("getHwInnerService", new Class[0]).invoke(objInvoke, new Object[0]));
        objInvoke2.getClass().getMethod("setHdbKey", String.class).invoke(objInvoke2, str);
        System.out.println("setHdbKey ok");
    }
}
