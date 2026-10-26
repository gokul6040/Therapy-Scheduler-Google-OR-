// Captured test-report shape, with Added_Time adapted to the user-confirmed IST contract.
// Both time fields use IST; all phone values are fictional.
// Only OAuth refresh and the two read-only report routes are implemented.
const http = require("http");
const records = {
  "Plan_Report": [
    {
      "Modified_Time": "21-Oct-2026 13:13:47",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-H",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016018",
      "PatientName": "Fictional Child H",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:13:47"
    },
    {
      "Modified_Time": "21-Oct-2026 13:13:09",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-G",
      "Schedule": "IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016016",
      "PatientName": "Fictional Child G",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:13:09"
    },
    {
      "Modified_Time": "21-Oct-2026 13:12:41",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-F",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016014",
      "PatientName": "Fictional Child F",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:12:41"
    },
    {
      "Modified_Time": "21-Oct-2026 19:26:09",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-E",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P; IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016012",
      "PatientName": "Fictional Child E",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:12:09"
    },
    {
      "Modified_Time": "21-Oct-2026 13:11:19",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-D",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016010",
      "PatientName": "Fictional Child D",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:11:19"
    },
    {
      "Modified_Time": "21-Oct-2026 13:10:50",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-C",
      "Schedule": "IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016008",
      "PatientName": "Fictional Child C",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:10:50"
    },
    {
      "Modified_Time": "21-Oct-2026 13:10:25",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-B",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016006",
      "PatientName": "Fictional Child B",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:10:25"
    },
    {
      "Modified_Time": "21-Oct-2026 19:25:55",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-A",
      "Schedule": "Psychology Therapy Session, 3, 3, TEST CONSULTANT P; IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016002",
      "PatientName": "Fictional Child A",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:08:51"
    }
  ],
  "All_Consultants": [
    {
      "Status": "Active",
      "Department": "IEP",
      "Modified_Time": "21-Oct-2026 13:16:34",
      "ConsultationType": "IEP Session",
      "Branch": "Malaparamba EIC",
      "Phone_Number": "919999999998",
      "ID": "504291000000016022",
      "Name": "TEST CONSULTANT I",
      "Added_Time": "21-Oct-2026 13:16:34"
    },
    {
      "Status": "Active",
      "Department": "Psychology",
      "Modified_Time": "21-Oct-2026 13:15:59",
      "ConsultationType": "Psychology Therapy Session",
      "Branch": "Malaparamba EIC",
      "Phone_Number": "919999999998",
      "ID": "504291000000016020",
      "Name": "TEST CONSULTANT P",
      "Added_Time": "21-Oct-2026 13:14:34"
    }
  ]
}
;
const wallTime = value => {
  const [day, month, year, hour, minute, second] = value.split(/[- :]/);
  return Date.UTC(Number(year), ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].indexOf(month), Number(day), Number(hour), Number(minute), Number(second));
};
const indiaWallTime = utcValue => {
  const date = new Date(wallTime(utcValue) + 330 * 60000);
  const pad = n => String(n).padStart(2, "0");
  return `${pad(date.getUTCDate())}-${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][date.getUTCMonth()]}-${date.getUTCFullYear()} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
};
for (const rows of Object.values(records)) {
  for (const row of rows) {
    row.Added_Time = indiaWallTime(row.Added_Time);
    row.Modified_Time = indiaWallTime(row.Modified_Time);
  }
}
records.Scheduler_Report = [{ID:"950000",Reg_ID:"DEMO",PatientName:"Fictional Existing Row",Doctor:"Demo Doctor",
  Therapy_Date:"24-10-2026",TherapyTime:"09:00 AM",Scheduler_Key:"",Status:"",Added_Time:"23-Oct-2026 11:05:33"}];
let schedulerSequence = 950000, failAfterCreate = false, schedulerKeyUnique = true;
http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  res.setHeader("Content-Type", "application/json");
  const schedulerBase = "/creator/v2.1/data/nidhin.kumar_turnb/test-gokul/";
  if (req.method === "GET" && url.pathname === "/creator/v2.1/meta/nidhin.kumar_turnb/test-gokul/form/Scheduler/fields") {
    res.end(JSON.stringify({code:3000,fields:["Reg_ID","PatientName","Doctor","Therapy_Date","TherapyTime","Scheduler_Key","Status"].map(link_name =>
      ({link_name,type:1,max_char:255,unique:link_name === "Scheduler_Key" && schedulerKeyUnique}))}));return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/scheduler-key-unique") {
    schedulerKeyUnique = req.body?.enabled === true;res.end(JSON.stringify({enabled:schedulerKeyUnique}));return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/fail-next-scheduler-create") {
    failAfterCreate = true; res.end(JSON.stringify({armed:true})); return;
  }
  if (req.method === "GET" && url.pathname === schedulerBase + "report/Scheduler_Report") {
    if (url.searchParams.get("field_config") !== "all" || url.searchParams.get("criteria") !== "ID != 0") {
      res.writeHead(400); res.end(JSON.stringify({code:3330})); return;
    }
    res.end(JSON.stringify({code:3000,data:records.Scheduler_Report})); return;
  }
  const schedulerUpdate = url.pathname.match(/^\/creator\/v2\.1\/data\/nidhin\.kumar_turnb\/test-gokul\/report\/Scheduler_Report\/(\d+)$/);
  if ((req.method === "POST" && url.pathname === schedulerBase + "form/Scheduler") || (req.method === "PATCH" && schedulerUpdate)) {
    const data = req.body?.data;
    const fields = ["Reg_ID","PatientName","Doctor","Therapy_Date","TherapyTime","Scheduler_Key","Status"];
    if (!data || Object.keys(data).sort().join() !== fields.sort().join() || !data.Scheduler_Key.startsWith("test-") || req.body.skip_workflow) {
      res.writeHead(400); res.end(JSON.stringify({code:3001})); return;
    }
    let row;
    if (schedulerUpdate) {
      row = records.Scheduler_Report.find(r => r.ID === schedulerUpdate[1] && r.Scheduler_Key === data.Scheduler_Key);
      if (!row) {res.writeHead(400);res.end(JSON.stringify({code:3100}));return;}
      Object.assign(row, data);
    } else {
      if (records.Scheduler_Report.some(r => r.Scheduler_Key === data.Scheduler_Key)) {
        res.writeHead(400);res.end(JSON.stringify({code:3001,message:"Duplicate Scheduler Key"}));return;
      }
      row = {ID:String(++schedulerSequence),...data,Added_Time:"23-Oct-2026 12:00:00"};
      records.Scheduler_Report.push(row);
      if (failAfterCreate) {failAfterCreate=false;res.writeHead(500);res.end(JSON.stringify({code:500}));return;}
    }
    res.end(JSON.stringify({code:3000,data:{ID:row.ID}}));return;
  }
  // Local fixture control, not a Zoho API route. Only dummy contact data is added.
  if (req.method === "POST" && url.pathname === "/__test__/add-patient") {
    const stamp = req.body?.at;
    const parent = req.body?.phone || "919999999999";
    if (typeof stamp !== "string" || !/^[1-9]\d{7,14}$/.test(parent)) {
      res.writeHead(400); res.end(JSON.stringify({message:"Supply at timestamp"})); return;
    }
    if (!records.Plan_Report.some(row => row.ID === "900001")) {
      records.Plan_Report.push({ID:"900001", RegistrationID:"NEW-009", PatientName:"New Fictional Child",
        Branch:"Malaparamba EIC", Parent_No:parent, Added_Time:indiaWallTime(stamp), Modified_Time:indiaWallTime(stamp),
        Schedule:"Psychology Therapy Session, 1, 3, New Clinician"});
      records.All_Consultants.push({ID:"900002", Name:"New Clinician", Branch:"Malaparamba EIC",
        Department:"Psychology", ConsultationType:"Psychology Therapy Session", Status:"Active",
        Phone_Number:"919999999998", Added_Time:indiaWallTime(stamp), Modified_Time:indiaWallTime(stamp)});
    }
    res.end(JSON.stringify({added:true})); return;
  }
  if (req.method === "POST" && url.pathname === "/oauth/v2/token") {
    const scope = "ZohoCreator.form.CREATE ZohoCreator.report.READ ZohoCreator.report.UPDATE ZohoCreator.meta.form.READ";
    const grant = req.body?.grant_type || new URLSearchParams(typeof req.body === "string" ? req.body : "").get("grant_type");
    res.end(JSON.stringify({access_token: "simulation-token", expires_in: 3600, scope,
      ...(grant === "authorization_code" ? {refresh_token:"simulation-refresh"} : {})})); return;
  }
  if (req.method === "GET" && url.pathname.startsWith("/creator/v2.1/data/nidhin.kumar_turnb/test-gokul/report/")) {
    const report = url.pathname.split("/").pop();
    if (!records[report]) { res.writeHead(501); res.end(JSON.stringify({message: "Endpoint not modeled"})); return; }
    if (url.searchParams.get("field_config") !== "all") {
      res.writeHead(400); res.end(JSON.stringify({message: "Expected field_config=all"})); return;
    }
    let data = records[report];
    const criteria = url.searchParams.get("criteria");
    if (criteria) {
      const addedEnd = criteria.match(/Added_Time < '([^']+)'/);
      const modifiedEnd = criteria.match(/Modified_Time < '([^']+)'/);
      const addedStart = criteria.match(/Added_Time >= '([^']+)'/);
      const modifiedStart = criteria.match(/Modified_Time >= '([^']+)'/);
      if (!addedEnd || !modifiedEnd || Boolean(addedStart) !== Boolean(modifiedStart)) {
        res.writeHead(400); res.end(JSON.stringify({code:3330,message:"Unsupported criteria"})); return;
      }
      data = data.filter(row => wallTime(row.Added_Time) < wallTime(addedEnd[1]) &&
        wallTime(row.Modified_Time) < wallTime(modifiedEnd[1]) &&
        (!addedStart || wallTime(row.Added_Time) >= wallTime(addedStart[1]) ||
          wallTime(row.Modified_Time) >= wallTime(modifiedStart[1])));
    }
    if (!data.length) {
      res.writeHead(400); res.end(JSON.stringify({code:9280,message:"No records found matching the given criteria."})); return;
    }
    if (report === "Plan_Report" && data.length > 4) {
      if (!req.headers.record_cursor) { res.setHeader("record_cursor", "next-page"); data = data.slice(0,4); }
      else if (req.headers.record_cursor === "next-page") data = data.slice(4);
      else { res.writeHead(400); res.end(JSON.stringify({message:"Bad cursor"})); return; }
    }
    res.end(JSON.stringify({code:3000,data})); return;
  }
  res.writeHead(501); res.end(JSON.stringify({message:"Endpoint not modeled"}));
}).listen(process.env.PORT || 4910);
