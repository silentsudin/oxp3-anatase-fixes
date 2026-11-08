// Report imported power/sleep APIs and the internal functions that call them.
// Our own helper (oxp3-anatase-fixes); used to check whether a vendor binary does any
// system-suspend / EC power programming of its own, or only millisecond delay sleeps.
//@category oxp3
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;
import java.nio.file.*;
import java.util.*;

public class PowerImports extends GhidraScript {
    public void run() throws Exception {
        String[] a = getScriptArgs();
        String out = a[0];
        StringBuilder sb = new StringBuilder();
        sb.append("# imported symbols matching power/sleep/suspend\n");
        Set<String> hit = new TreeSet<>();
        for (Symbol s : currentProgram.getSymbolTable().getExternalSymbols()) {
            String n = s.getName();
            if (n.matches("(?i).*(sleep|suspend|hibernate|power|acpi|ec_|ecw?|battery|d3|s0ix|standby|wake).*"))
                hit.add(n);
        }
        for (String n : hit) sb.append("import ").append(n).append("\n");

        sb.append("\n# functions that reference those imports (decompiled)\n");
        DecompInterface d = new DecompInterface();
        d.openProgram(currentProgram);
        Set<Function> callers = new LinkedHashSet<>();
        for (Symbol s : currentProgram.getSymbolTable().getExternalSymbols()) {
            if (!hit.contains(s.getName())) continue;
            ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(s.getAddress());
            while (it.hasNext()) {
                Reference r = it.next();
                Function f = getFunctionContaining(r.getFromAddress());
                if (f != null) callers.add(f);
            }
        }
        int ok = 0;
        for (Function f : callers) {
            sb.append("== ").append(f.getName()).append(" @ ").append(f.getEntryPoint()).append("\n");
            DecompileResults r = d.decompileFunction(f, 60, monitor);
            if (r != null && r.decompileCompleted()) {
                String c = r.getDecompiledFunction().getC();
                sb.append(c.length() > 12000 ? c.substring(0, 12000) + "\n// [truncated]\n" : c).append("\n");
                ok++;
            } else {
                sb.append("// decompile failed\n");
            }
        }
        Files.writeString(Path.of(out), sb.toString());
        println("WROTE " + out + " imports=" + hit.size() + " callers=" + callers.size() + " decompiled=" + ok);
    }
}
