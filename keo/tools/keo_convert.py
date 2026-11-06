#!/usr/bin/env python3
"""KEO converter: turns a big STEP / IGES / BREP assembly into a small .keomesh file
that KEO opens in a couple of seconds.

Why: a browser tab can't hold a 100+ MB CAD file in memory. This script does the heavy
reading and meshing once, on your computer, with the native OpenCascade kernel.

Setup (once):   pip install cadquery numpy
Run:            python keo_convert.py ASSEMBLY_VTOL.stp
                python keo_convert.py ASSEMBLY_VTOL.stp --detail coarse    (smaller, faster)
Detail levels:  coarse | normal (default) | fine
Then drop the .keomesh file onto KEO (or press Import).
"""
import sys, os, time, json, struct, argparse
import numpy as np

def log(*a):
    print(*a, flush=True)

def main():
    ap = argparse.ArgumentParser(description="Convert a big STEP/IGES/BREP file to a KEO .keomesh file")
    ap.add_argument("input")
    ap.add_argument("--detail", choices=["coarse", "normal", "fine"], default="normal")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-parts", type=int, default=4000)
    a = ap.parse_args()
    t0 = time.time()
    try:
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.IGESCAFControl import IGESCAFControl_Reader
        from OCP.TDocStd import TDocStd_Document
        from OCP.XCAFApp import XCAFApp_Application
        from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool, XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_ColorCurv
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDF import TDF_LabelSequence, TDF_Label
        from OCP.TDataStd import TDataStd_Name
        from OCP.Quantity import Quantity_Color
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.BRep import BRep_Tool
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.IFSelect import IFSelect_RetDone
    except ImportError:
        log("Missing the OpenCascade bindings. Install them with:  pip install cadquery numpy")
        sys.exit(1)

    path = a.input
    if not os.path.isfile(path):
        log("File not found:", path); sys.exit(1)
    ext = os.path.splitext(path)[1].lower()
    log("Reading %s (%.1f MB). This is the slow step: a 140 MB assembly takes a few minutes." % (os.path.basename(path), os.path.getsize(path) / 1e6))

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    if ext in (".iges", ".igs"):
        rd = IGESCAFControl_Reader()
    elif ext in (".brep", ".brp"):
        rd = None
    else:
        rd = STEPCAFControl_Reader()
    shape_tool = None
    parts = []  # (name, TopoDS_Shape, rgb|None)

    if rd is None:
        from OCP.BRepTools import BRepTools
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Shape
        sh = TopoDS_Shape(); BRepTools.Read_s(sh, path, BRep_Builder())
        parts.append((os.path.splitext(os.path.basename(path))[0], sh, None))
    else:
        rd.SetColorMode(True); rd.SetNameMode(True)
        if rd.ReadFile(path) != IFSelect_RetDone:
            log("The CAD file could not be read (is it a valid STEP/IGES file?)."); sys.exit(1)
        log("  file parsed in %.0f s, building the part tree ..." % (time.time() - t0))
        if not rd.Transfer(doc):
            log("The CAD file could not be transferred."); sys.exit(1)
        log("  part tree built in %.0f s" % (time.time() - t0))
        main_lab = doc.Main()
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(main_lab)
        color_tool = XCAFDoc_DocumentTool.ColorTool_s(main_lab)

        def name_of(lab):
            at = TDataStd_Name()
            if lab.FindAttribute(TDataStd_Name.GetID_s(), at):
                try:
                    s = at.Get().ToExtString()
                    return str(s)
                except Exception:
                    return None
            return None

        def colour_of(lab):
            c = Quantity_Color()
            for t in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv):
                try:
                    if color_tool.GetColor(lab, t, c):
                        return (c.Red(), c.Green(), c.Blue())
                except Exception:
                    pass
            return None

        def face_colour(so):
            # colour attached to the faces of a solid (common in NX / CATIA exports)
            try:
                ex3 = TopExp_Explorer(so, TopAbs_FACE)
                c = Quantity_Color()
                k = 0
                while ex3.More() and k < 6:
                    if color_tool.GetColor(ex3.Current(), XCAFDoc_ColorSurf, c) or color_tool.GetColor(ex3.Current(), XCAFDoc_ColorGen, c):
                        return (c.Red(), c.Green(), c.Blue())
                    ex3.Next(); k += 1
            except Exception:
                pass
            return None

        def walk(lab, loc, nm, col, depth=0):
            if depth > 60:
                return
            n2 = name_of(lab) or nm
            c2 = colour_of(lab) or col
            if XCAFDoc_ShapeTool.IsAssembly_s(lab):
                comps = TDF_LabelSequence()
                XCAFDoc_ShapeTool.GetComponents_s(lab, comps)
                for i in range(1, comps.Length() + 1):
                    comp = comps.Value(i)
                    ref = TDF_Label()
                    if XCAFDoc_ShapeTool.GetReferredShape_s(comp, ref):
                        l2 = loc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(comp))
                        walk(ref, l2, name_of(comp) or n2, colour_of(comp) or c2, depth + 1)
            else:
                sh = XCAFDoc_ShapeTool.GetShape_s(lab)
                if sh.IsNull():
                    return
                sh = sh.Moved(loc)
                # many CAD systems store a whole assembly as ONE product holding hundreds of solids:
                # split those into separate parts so they can be exploded, hidden and edited one by one
                sols = []
                ex2 = TopExp_Explorer(sh, TopAbs_SOLID)
                while ex2.More():
                    sols.append(ex2.Current()); ex2.Next()
                if len(sols) > 1:
                    for si, so in enumerate(sols):
                        sl = TDF_Label(); nm2 = None; cl2 = None
                        try:
                            if shape_tool.FindShape(so, sl, False) or shape_tool.FindShape(so, sl, True):
                                nm2 = name_of(sl); cl2 = colour_of(sl)
                        except Exception:
                            pass
                        if cl2 is None:
                            cl2 = face_colour(so)
                        parts.append(((nm2 or ("%s %d" % (n2 or "Part", si + 1))), so, cl2 or c2))
                else:
                    parts.append((n2 or "Part", sh, c2))

        free = TDF_LabelSequence()
        shape_tool.GetFreeShapes(free)
        for i in range(1, free.Length() + 1):
            walk(free.Value(i), TopLoc_Location(), None, None)
    if not parts:
        log("No solid geometry found in that file."); sys.exit(1)
    log("  %d parts found (%.0f s). Meshing ..." % (len(parts), time.time() - t0))

    bb = Bnd_Box()
    for _, sh, _ in parts:
        BRepBndLib.Add_s(sh, bb)
    x0, y0, z0, x1, y1, z1 = bb.Get()
    diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    f = {"coarse": 0.004, "normal": 0.0015, "fine": 0.0006}[a.detail]
    glob = diag * f

    out_parts = []
    ntri = 0
    for k, (nm, sh, col) in enumerate(parts):
        pb = Bnd_Box(); BRepBndLib.Add_s(sh, pb)
        try:
            px0, py0, pz0, px1, py1, pz1 = pb.Get()
            pd = ((px1 - px0) ** 2 + (py1 - py0) ** 2 + (pz1 - pz0) ** 2) ** 0.5
        except Exception:
            pd = diag
        defl = max(min(glob, pd * 0.03), glob * 0.1)
        BRepMesh_IncrementalMesh(sh, defl, False, 0.5, True)
        P = []; I = []; base = 0
        ex = TopExp_Explorer(sh, TopAbs_FACE)
        while ex.More():
            face = TopoDS.Face_s(ex.Current())
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            if tri is not None:
                tr = loc.Transformation()
                nn = tri.NbNodes()
                pts = np.empty((nn, 3), dtype=np.float64)
                for i in range(1, nn + 1):
                    p = tri.Node(i).Transformed(tr)
                    pts[i - 1] = (p.X(), p.Y(), p.Z())
                nt = tri.NbTriangles()
                idx = np.empty((nt, 3), dtype=np.int64)
                rev = face.Orientation() == TopAbs_REVERSED
                for i in range(1, nt + 1):
                    a1, b1, c1 = tri.Triangle(i).Get()
                    idx[i - 1] = (a1 - 1, c1 - 1, b1 - 1) if rev else (a1 - 1, b1 - 1, c1 - 1)
                P.append(pts); I.append(idx + base); base += nn
            ex.Next()
        if not P:
            continue
        pos = np.concatenate(P).astype(np.float32)
        idx = np.concatenate(I).astype(np.uint32)
        # smooth vertex normals (per face, so hard edges stay hard)
        v0, v1, v2 = pos[idx[:, 0]], pos[idx[:, 1]], pos[idx[:, 2]]
        fn = np.cross(v1 - v0, v2 - v0)
        nrm = np.zeros_like(pos)
        for j in range(3):
            np.add.at(nrm, idx[:, j], fn)
        ln = np.linalg.norm(nrm, axis=1); ln[ln == 0] = 1
        nrm = (nrm / ln[:, None]).astype(np.float32)
        ntri += len(idx)
        out_parts.append((str(nm)[:40], col, pos, idx, nrm))
        if (k + 1) % 25 == 0 or k + 1 == len(parts):
            log("  meshed %d / %d parts, %s triangles (%.0f s)" % (k + 1, len(parts), format(ntri, ","), time.time() - t0))

    # merge the smallest parts if there are too many
    if len(out_parts) > a.max_parts:
        out_parts.sort(key=lambda p: -len(p[3]))
        keep, rest = out_parts[:a.max_parts - 1], out_parts[a.max_parts - 1:]
        pos = np.concatenate([p[2] for p in rest]); nrm = np.concatenate([p[4] for p in rest])
        off = 0; I = []
        for p in rest:
            I.append(p[3] + off); off += len(p[2])
        keep.append(("Other parts", None, pos, np.concatenate(I).astype(np.uint32), nrm))
        out_parts = keep
        log("  merged the smallest parts into 'Other parts' (limit %d)" % a.max_parts)

    out = a.out or os.path.splitext(path)[0] + ".keomesh"
    meta = [{"n": p[0], "c": ([round(float(v), 4) for v in p[1]] if p[1] else None), "nv": int(len(p[2])), "ni": int(p[3].size)} for p in out_parts]
    js = json.dumps(meta, ensure_ascii=True).encode("ascii")
    js += b" " * ((-len(js)) % 4)
    with open(out, "wb") as fh:
        fh.write(b"KEOMESH1"); fh.write(struct.pack("<II", len(out_parts), len(js))); fh.write(js)
        for p in out_parts:
            fh.write(p[2].astype("<f4").tobytes()); fh.write(p[4].astype("<f4").tobytes()); fh.write(p[3].astype("<u4").tobytes())
    log("Done: %s  (%.1f MB, %d parts, %s triangles, %.0f s). Drop it onto KEO." % (out, os.path.getsize(out) / 1e6, len(out_parts), format(ntri, ","), time.time() - t0))

if __name__ == "__main__":
    main()
