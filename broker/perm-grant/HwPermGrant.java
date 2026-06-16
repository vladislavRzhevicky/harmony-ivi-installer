public final class HwPermGrant {
    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("usage: HwPermGrant <pkg> <userId> <perm1> [perm2 ...]");
            System.exit(2);
        }
        String pkg = args[0];
        int userId = Integer.parseInt(args[1]);

        Object svc = Class.forName("android.os.ServiceManager")
                .getMethod("getService", String.class).invoke(null, "package");
        Object pm = Class.forName("android.content.pm.IPackageManager$Stub")
                .getMethod("asInterface", Class.forName("android.os.IBinder"))
                .invoke(null, svc);

        java.lang.reflect.Method grant = null;
        // Try the 3-arg signature first (pkg, perm, userId).
        try {
            grant = pm.getClass().getMethod("grantRuntimePermission",
                    String.class, String.class, int.class);
        } catch (NoSuchMethodException ignore) {}
        // Fallback to 4-arg signature (pkg, perm, persistentDeviceId, userId).
        if (grant == null) {
            for (java.lang.reflect.Method m : pm.getClass().getMethods()) {
                if ("grantRuntimePermission".equals(m.getName())) {
                    grant = m;
                    break;
                }
            }
        }
        if (grant == null) {
            System.err.println("no grantRuntimePermission method on IPackageManager");
            System.exit(3);
        }
        Class<?>[] sig = grant.getParameterTypes();
        System.out.println("using " + grant);

        int ok = 0, fail = 0;
        for (int i = 2; i < args.length; i++) {
            String perm = args[i];
            try {
                if (sig.length == 3) {
                    grant.invoke(pm, pkg, perm, userId);
                } else if (sig.length == 4 && sig[2] == String.class) {
                    grant.invoke(pm, pkg, perm, null, userId);
                } else if (sig.length == 4) {
                    grant.invoke(pm, pkg, perm, userId, null);
                } else {
                    throw new IllegalStateException("unexpected signature: " + grant);
                }
                System.out.println("granted: " + perm);
                ok++;
            } catch (Throwable t) {
                Throwable c = t.getCause() != null ? t.getCause() : t;
                System.out.println("failed " + perm + ": "
                        + c.getClass().getName() + ": " + c.getMessage());
                fail++;
            }
        }
        System.out.println("summary: ok=" + ok + " fail=" + fail);
    }
}
