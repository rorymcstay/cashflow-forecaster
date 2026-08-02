Attribute VB_Name = "CashFlowMacros"
Option Explicit

' Archives the current month's Cash Flow Forecast as a static snapshot sheet,
' then rolls the live sheet forward to next month using the closing effective
' balance as the new opening balance. Run once a month (or whenever you're
' ready to move on).
Sub BuildNextMonthForecast()
    Dim wsCF As Worksheet, wsArchive As Worksheet, wsTest As Worksheet
    Dim archiveName As String
    Dim closingBalance As Double
    Dim newMonth As Date

    Set wsCF = ThisWorkbook.Sheets("Cash Flow Forecast")
    archiveName = Format(wsCF.Range("B4").Value, "MMM yyyy")

    ' Bail out if this month has already been archived
    On Error Resume Next
    Set wsTest = ThisWorkbook.Sheets(archiveName)
    On Error GoTo 0
    If Not wsTest Is Nothing Then
        MsgBox "An archive sheet named '" & archiveName & "' already exists." & vbNewLine & _
               "Rename or delete it first if you want to re-run this.", vbExclamation
        Exit Sub
    End If

    ' 1. Snapshot the current month as static values on a new archive sheet
    wsCF.Copy After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count)
    Set wsArchive = ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count)
    wsArchive.Name = archiveName
    With wsArchive.UsedRange
        .Value = .Value
    End With

    ' 2. Capture this month's closing effective balance before rolling forward
    closingBalance = wsCF.Range("ClosingEffectiveBalance").Value

    ' 3. Roll the live sheet forward to next month
    newMonth = DateSerial(Year(wsCF.Range("B4").Value), Month(wsCF.Range("B4").Value) + 1, 1)
    wsCF.Range("B4").Value = newMonth
    wsCF.Range("E4").Value = closingBalance

    ' 4. Clear manual Realised Net entries so the new month starts blank.
    ' Upcoming Expenses is a persistent multi-month list, so it is left alone —
    ' items simply stop showing up once their date is no longer in the current
    ' forecast month.
    wsCF.Range("G7:G37").ClearContents

    MsgBox "Archived as '" & archiveName & "'." & vbNewLine & _
           "Forecast rolled to " & Format(newMonth, "MMMM yyyy") & _
           " with opening balance " & Format(closingBalance, "£#,##0.00"), vbInformation
End Sub
