// Decompile functions matching an optional regex into a text file. Our own helper (oxp3-anatase-fixes).
//@category oxp3
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import java.nio.file.*;

public class DumpAll extends GhidraScript {
    public void run() throws Exception {
        String[] a = getScriptArgs();
        String out = a[0];
        String pat = a.length > 1 ? a[1] : "";
        DecompInterface d = new DecompInterface();
        d.openProgram(currentProgram);
        StringBuilder sb = new StringBuilder();
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        int n = 0, ok = 0;
        while (it.hasNext()) {
            Function f = it.next();
            if (!pat.isEmpty() && !f.getName().matches(pat)) continue;
            n++;
            sb.append("== ").append(f.getName()).append(" @ ").append(f.getEntryPoint()).append("\n");
            DecompileResults r = d.decompileFunction(f, 90, monitor);
            if (r != null && r.decompileCompleted()) {
                sb.append(r.getDecompiledFunction().getC()).append("\n");
                ok++;
            } else {
                sb.append("// decompile failed\n");
            }
        }
        Files.writeString(Path.of(out), sb.toString());
        println("WROTE " + out + " functions=" + n + " decompiled=" + ok);
    }
}
