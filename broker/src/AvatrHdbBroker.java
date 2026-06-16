package defpackage;

import android.util.Base64;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/* JADX INFO: loaded from: classes.dex */
public final class AvatrHdbBroker {
    private static final int BASE64_FLAGS = 10;

    private AvatrHdbBroker() {
    }

    public static void main(String[] strArr) throws Exception {
        int i;
        if (strArr.length > 0 && strArr[0] != null && !strArr[0].trim().isEmpty()) {
            i = Integer.parseInt(strArr[0].trim());
        } else {
            i = 38787;
        }
        ServerSocket serverSocket = new ServerSocket(i, 50, InetAddress.getByName("127.0.0.1"));
        System.out.println("AvatrHdbBroker listening tcp=127.0.0.1:" + i);
        System.out.flush();
        while (true) {
            handle(serverSocket.accept());
        }
    }

    private static void handle(Socket socket) {
        InputStream inputStream;
        OutputStreamWriter outputStreamWriter;
        String line;
        try {
            inputStream = socket.getInputStream();
            outputStreamWriter = new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8);
            line = readLine(inputStream);
        } catch (Throwable th) {
            try {
                OutputStreamWriter outputStreamWriter2 = new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8);
                ByteArrayOutputStream byteArrayOutputStream = new ByteArrayOutputStream();
                th.printStackTrace(new PrintStream((OutputStream) byteArrayOutputStream, true));
                writeResult(outputStreamWriter2, 1, byteArrayOutputStream.toString());
            } catch (Throwable th2) {
            }
        }
        if (line != null && !line.trim().isEmpty()) {
            if ("PING".equals(line.trim())) {
                writeResult(outputStreamWriter, 0, "pong\n");
                try {
                    socket.close();
                    return;
                } catch (Throwable th3) {
                    return;
                }
            } else if (line.startsWith("STAGE ")) {
                stageFile(inputStream, outputStreamWriter, line.substring(6));
                try {
                    socket.close();
                    return;
                } catch (Throwable th4) {
                    return;
                }
            } else if (!line.startsWith("RUN ")) {
                writeResult(outputStreamWriter, 2, "Unsupported broker command.\n");
                try {
                    socket.close();
                    return;
                } catch (Throwable th5) {
                    return;
                }
            } else {
                BridgeResult bridgeResultRunBridge = runBridge((String[]) decodeArguments(line.substring(4)).toArray(new String[0]));
                writeResult(outputStreamWriter, bridgeResultRunBridge.exitCode, bridgeResultRunBridge.output);
                try {
                    socket.close();
                    return;
                } catch (Throwable th6) {
                    return;
                }
            }
        }
        writeResult(outputStreamWriter, 2, "Empty broker command.\n");
        try {
            socket.close();
        } catch (Throwable th7) {
        }
    }

    private static String readLine(InputStream inputStream) throws Exception {
        ByteArrayOutputStream byteArrayOutputStream = new ByteArrayOutputStream();
        while (true) {
            int i = inputStream.read();
            if (i == -1) {
                if (byteArrayOutputStream.size() == 0) {
                    return null;
                }
                return byteArrayOutputStream.toString(StandardCharsets.UTF_8.name());
            }
            if (i == BASE64_FLAGS) {
                return byteArrayOutputStream.toString(StandardCharsets.UTF_8.name());
            }
            if (i != 13) {
                byteArrayOutputStream.write(i);
            }
        }
    }

    private static void stageFile(InputStream inputStream, OutputStreamWriter outputStreamWriter, String str) throws Exception {
        String str2;
        String str3 = " ";
        String[] strArrSplit = str.trim().split(" ");
        if (strArrSplit.length != 2) {
            writeResult(outputStreamWriter, 2, "Malformed STAGE command.\n");
            return;
        }
        String strReplaceAll = new String(Base64.decode(strArrSplit[0], BASE64_FLAGS), StandardCharsets.UTF_8).replace('\\', '/').replaceAll("/+", "/");
        long j = Long.parseLong(strArrSplit[1]);
        long j2 = 0;
        if (j < 0) {
            writeResult(outputStreamWriter, 2, "Negative STAGE length.\n");
            return;
        }
        if (strReplaceAll.startsWith("/") || strReplaceAll.contains("../") || strReplaceAll.equals("..") || strReplaceAll.contains("/..")) {
            writeResult(outputStreamWriter, 2, "Unsafe STAGE path.\n");
            return;
        }
        File canonicalFile = new File("/data/local/tmp/avatr-hdb-stage").getCanonicalFile();
        File canonicalFile2 = new File(canonicalFile, strReplaceAll).getCanonicalFile();
        if (!canonicalFile2.getPath().startsWith(canonicalFile.getPath() + File.separator)) {
            writeResult(outputStreamWriter, 2, "STAGE path escaped staging root.\n");
            return;
        }
        File parentFile = canonicalFile2.getParentFile();
        if (parentFile != null && !parentFile.isDirectory() && !parentFile.mkdirs()) {
            writeResult(outputStreamWriter, 1, "Could not create STAGE directory: " + parentFile.getPath() + "\n");
            return;
        }
        int i = 65536;
        byte[] bArr = new byte[65536];
        FileOutputStream fileOutputStream = new FileOutputStream(canonicalFile2);
        while (true) {
            if (j2 >= j) {
                str2 = str3;
                break;
            }
            str2 = str3;
            try {
                int i2 = inputStream.read(bArr, 0, (int) Math.min(i, j - j2));
                if (i2 == -1) {
                    break;
                }
                fileOutputStream.write(bArr, 0, i2);
                j2 += (long) i2;
                str3 = str2;
                i = 65536;
            } finally {
            }
        }
        fileOutputStream.getFD().sync();
        fileOutputStream.close();
        if (j2 != j) {
            canonicalFile2.delete();
            writeResult(outputStreamWriter, 1, "STAGE byte count mismatch: expected " + j + " got " + j2 + "\n");
            return;
        }
        canonicalFile2.setReadable(true, false);
        canonicalFile2.setWritable(true, false);
        outputStreamWriter.write("STAGED " + Base64.encodeToString(canonicalFile2.getPath().getBytes(StandardCharsets.UTF_8), BASE64_FLAGS) + str2 + j2 + "\n");
        outputStreamWriter.flush();
    }

    private static BridgeResult runBridge(String[] strArr) {
        BridgeResult bridgeResult;
        synchronized (AvatrHdbBroker.class) {
            ByteArrayOutputStream byteArrayOutputStream = new ByteArrayOutputStream();
            PrintStream printStream = System.out;
            PrintStream printStream2 = System.err;
            int i = 1;
            PrintStream printStream3 = new PrintStream((OutputStream) byteArrayOutputStream, true);
            try {
                System.setOut(printStream3);
                System.setErr(printStream3);
                HuaweiShellBridge.main(strArr);
                printStream3.flush();
                System.setOut(printStream);
                System.setErr(printStream2);
                i = 0;
            } catch (Throwable th) {
                try {
                    th.printStackTrace(printStream3);
                } finally {
                    printStream3.flush();
                    System.setOut(printStream);
                    System.setErr(printStream2);
                }
            }
            bridgeResult = new BridgeResult(i, byteArrayOutputStream.toString());
        }
        return bridgeResult;
    }

    private static List<String> decodeArguments(String str) {
        String[] strArrSplit = str.trim().isEmpty() ? new String[0] : str.trim().split(" ");
        ArrayList arrayList = new ArrayList(strArrSplit.length);
        for (String str2 : strArrSplit) {
            if (str2 != null && !str2.isEmpty()) {
                arrayList.add(new String(Base64.decode(str2, BASE64_FLAGS), StandardCharsets.UTF_8));
            }
        }
        return arrayList;
    }

    private static void writeResult(OutputStreamWriter outputStreamWriter, int i, String str) throws Exception {
        if (str == null) {
            str = "";
        }
        outputStreamWriter.write("RESULT " + i + " " + Base64.encodeToString(str.getBytes(StandardCharsets.UTF_8), BASE64_FLAGS) + "\n");
        outputStreamWriter.flush();
    }

    private static final class BridgeResult {
        final int exitCode;
        final String output;

        BridgeResult(int i, String str) {
            this.exitCode = i;
            this.output = str == null ? "" : str;
        }
    }
}
