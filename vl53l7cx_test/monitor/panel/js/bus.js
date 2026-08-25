// Event bus (C03): fans out parsed SSE events to a dataStore (ring buffer,
// always running) and to the active mode's render() (only the visible mode
// runs). See ssi-backlog/README.md, "架構關鍵：資料層與模式層分離".
//
// C01 only reserves this file per the module layout in C01.md; the actual
// bus/dataStore implementation belongs to C03.
