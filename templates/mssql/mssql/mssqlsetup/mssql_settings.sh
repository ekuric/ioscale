## Setting control parameters 
/opt/mssql/bin/mssql-conf set control.alternatewritethrough 0
/opt/mssql/bin/mssql-conf set control.writethrough 0

## Setting memory parameters
/opt/mssql/bin/mssql-conf set memory.disablememorypressure true

## Setting Traceflags
/opt/mssql/bin/mssql-conf traceflag 652 on
/opt/mssql/bin/mssql-conf traceflag 823 on
/opt/mssql/bin/mssql-conf traceflag 834 on
/opt/mssql/bin/mssql-conf traceflag 880 on
/opt/mssql/bin/mssql-conf traceflag 1224 on
/opt/mssql/bin/mssql-conf traceflag 1613 on
/opt/mssql/bin/mssql-conf traceflag 2330 on
/opt/mssql/bin/mssql-conf traceflag 3468 on
/opt/mssql/bin/mssql-conf traceflag 3979 on
/opt/mssql/bin/mssql-conf traceflag 7413 on
/opt/mssql/bin/mssql-conf traceflag 8040 on
/opt/mssql/bin/mssql-conf traceflag 8077 on
/opt/mssql/bin/mssql-conf traceflag 8088 on
/opt/mssql/bin/mssql-conf traceflag 8744 on


