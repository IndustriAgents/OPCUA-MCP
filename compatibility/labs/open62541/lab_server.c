/* Conformance lab server built on the open62541 SDK (#147).
 *
 * open62541's own example servers each show one feature. The conformance
 * harness needs one server that has all of them at once, configured the way a
 * real deployment is — a trust list rather than "accept everything", a user who
 * may not write every node, operation limits small enough that a client has to
 * respect them — so this file assembles the SDK's documented building blocks
 * into that. The *stack* is what is under test: every OPC UA service below is
 * answered by open62541, not by code here. Nothing in this file is shipped.
 *
 * Usage:
 *   lab_server --port 4850 --cert server.der --key server_key.der
 *              [--trust client.der ...] [--pad-namespace]
 *
 * The username and password come from OPCUA_CONFORMANCE_USERNAME and
 * OPCUA_CONFORMANCE_PASSWORD, the same variables the harness config names, so
 * no credential is ever written into a file or onto a command line.
 *
 * --pad-namespace registers an extra namespace *before* the lab's own, so its
 * index moves from 2 to 3: the harness restarts the server with it to check
 * that neither runtime carries a stale NamespaceArray across a reconnect. */

#include <open62541/plugin/accesscontrol_default.h>
#include <open62541/plugin/historydata/history_data_backend_memory.h>
#include <open62541/plugin/historydata/history_data_gathering_default.h>
#include <open62541/plugin/historydata/history_database_default.h>
#include <open62541/plugin/log_stdout.h>
#include <open62541/server.h>
#include <open62541/server_config_default.h>

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define LAB_NAMESPACE "urn:opcua-mcp:conformance-lab"
#define MAX_TRUST 8
/* Big enough that a browse of it cannot be answered in one response once
 * maxReferencesPerNode is applied, so continuation points are exercised. */
#define LARGE_FOLDER_SIZE 1200

static UA_UInt16 ns;
static UA_Double rampValue = 0.0;
static UA_NodeId alarmCondition;
static UA_NodeId alarmSource;

static UA_ByteString
loadFile(const char *path) {
    UA_ByteString content = UA_BYTESTRING_NULL;
    FILE *fp = fopen(path, "rb");
    if(!fp)
        return content;
    fseek(fp, 0, SEEK_END);
    long size = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if(size > 0 && UA_ByteString_allocBuffer(&content, (size_t)size) == UA_STATUSCODE_GOOD) {
        if(fread(content.data, 1, (size_t)size, fp) != (size_t)size)
            UA_ByteString_clear(&content);
    }
    fclose(fp);
    return content;
}

static UA_NodeId
labId(const char *path) {
    return UA_NODEID_STRING(ns, (char *)(uintptr_t)path);
}

static UA_NodeId
addFolder(UA_Server *server, const char *path, const char *name, UA_NodeId parent) {
    UA_ObjectAttributes attr = UA_ObjectAttributes_default;
    attr.displayName = UA_LOCALIZEDTEXT("en", (char *)(uintptr_t)name);
    UA_NodeId out;
    UA_Server_addObjectNode(server, labId(path), parent, UA_NS0ID(ORGANIZES),
                            UA_QUALIFIEDNAME(ns, (char *)(uintptr_t)name),
                            UA_NS0ID(FOLDERTYPE), attr, NULL, &out);
    return out;
}

static void
addVariable(UA_Server *server, UA_NodeId parent, const char *path, const char *name,
            const UA_DataType *type, const void *value, size_t arrayLength,
            UA_Byte accessLevel) {
    UA_VariableAttributes attr = UA_VariableAttributes_default;
    attr.displayName = UA_LOCALIZEDTEXT("en", (char *)(uintptr_t)name);
    attr.dataType = type->typeId;
    attr.accessLevel = accessLevel;
    if(arrayLength > 0) {
        UA_Variant_setArray(&attr.value, (void *)(uintptr_t)value, arrayLength, type);
        attr.valueRank = UA_VALUERANK_ONE_DIMENSION;
        UA_UInt32 dims[1] = {0};
        attr.arrayDimensions = dims;
        attr.arrayDimensionsSize = 1;
    } else {
        UA_Variant_setScalar(&attr.value, (void *)(uintptr_t)value, type);
        attr.valueRank = UA_VALUERANK_SCALAR;
    }
    UA_Server_addVariableNode(server, labId(path), parent, UA_NS0ID(HASCOMPONENT),
                              UA_QUALIFIEDNAME(ns, (char *)(uintptr_t)name),
                              UA_NS0ID(BASEDATAVARIABLETYPE), attr, NULL, NULL);
}

static void
addValues(UA_Server *server, UA_NodeId root) {
    const UA_Byte R = UA_ACCESSLEVELMASK_READ;
    const UA_Byte RW = UA_ACCESSLEVELMASK_READ | UA_ACCESSLEVELMASK_WRITE;

    UA_NodeId scalars = addFolder(server, "Scalars", "Scalars", root);
    UA_Boolean b = true;
    UA_Int32 i32 = -42;
    UA_UInt16 u16 = 7;
    UA_Int64 i64 = 9007199254740993LL; /* one past 2^53: not exact as a double */
    UA_Float f = 1.5f;
    UA_Double d = 2.25;
    UA_String s = UA_STRING("lab");
    UA_DateTime dt = UA_DateTime_now();
    UA_ByteString bs = UA_BYTESTRING("\x01\x02\x03");
    UA_Guid g = UA_Guid_random();
    UA_LocalizedText lt = UA_LOCALIZEDTEXT("en", "text");
    addVariable(server, scalars, "Scalars/Boolean", "Boolean", &UA_TYPES[UA_TYPES_BOOLEAN], &b, 0, R);
    addVariable(server, scalars, "Scalars/Int32", "Int32", &UA_TYPES[UA_TYPES_INT32], &i32, 0, R);
    addVariable(server, scalars, "Scalars/UInt16", "UInt16", &UA_TYPES[UA_TYPES_UINT16], &u16, 0, R);
    addVariable(server, scalars, "Scalars/Int64", "Int64", &UA_TYPES[UA_TYPES_INT64], &i64, 0, R);
    addVariable(server, scalars, "Scalars/Float", "Float", &UA_TYPES[UA_TYPES_FLOAT], &f, 0, R);
    addVariable(server, scalars, "Scalars/Double", "Double", &UA_TYPES[UA_TYPES_DOUBLE], &d, 0, R);
    addVariable(server, scalars, "Scalars/String", "String", &UA_TYPES[UA_TYPES_STRING], &s, 0, R);
    addVariable(server, scalars, "Scalars/DateTime", "DateTime", &UA_TYPES[UA_TYPES_DATETIME], &dt, 0, R);
    addVariable(server, scalars, "Scalars/ByteString", "ByteString", &UA_TYPES[UA_TYPES_BYTESTRING], &bs, 0, R);
    addVariable(server, scalars, "Scalars/Guid", "Guid", &UA_TYPES[UA_TYPES_GUID], &g, 0, R);
    addVariable(server, scalars, "Scalars/LocalizedText", "LocalizedText", &UA_TYPES[UA_TYPES_LOCALIZEDTEXT], &lt, 0, R);

    UA_NodeId arrays = addFolder(server, "Arrays", "Arrays", root);
    UA_Double da[4] = {1.0, 2.0, 3.0, 4.0};
    UA_Int32 ia[3] = {1, -2, 3};
    UA_Boolean ba[2] = {true, false};
    UA_String sa[2] = {UA_STRING("a"), UA_STRING("b")};
    addVariable(server, arrays, "Arrays/Double", "Double", &UA_TYPES[UA_TYPES_DOUBLE], da, 4, R);
    addVariable(server, arrays, "Arrays/Int32", "Int32", &UA_TYPES[UA_TYPES_INT32], ia, 3, R);
    addVariable(server, arrays, "Arrays/Boolean", "Boolean", &UA_TYPES[UA_TYPES_BOOLEAN], ba, 2, R);
    addVariable(server, arrays, "Arrays/String", "String", &UA_TYPES[UA_TYPES_STRING], sa, 2, R);

    /* Structures from namespace 0, so both client libraries have the type
     * definition built in; a custom structure is a separate question. */
    UA_NodeId structures = addFolder(server, "Structures", "Structures", root);
    UA_Range range = {-50.0, 250.0};
    UA_EUInformation eu;
    UA_EUInformation_init(&eu);
    eu.namespaceUri = UA_STRING("http://www.opcfoundation.org/UA/units/un/cefact");
    eu.unitId = 4408652;
    eu.displayName = UA_LOCALIZEDTEXT("en", "\xc2\xb0""C");
    eu.description = UA_LOCALIZEDTEXT("en", "degree Celsius");
    addVariable(server, structures, "Structures/Range", "Range", &UA_TYPES[UA_TYPES_RANGE], &range, 0, R);
    addVariable(server, structures, "Structures/EUInformation", "EUInformation", &UA_TYPES[UA_TYPES_EUINFORMATION], &eu, 0, R);

    /* The only nodes a write scenario may touch. */
    UA_NodeId scratch = addFolder(server, "Scratch", "Scratch", root);
    UA_Double sd = 0.0;
    UA_Int32 si = 0;
    addVariable(server, scratch, "Scratch/Double", "Double", &UA_TYPES[UA_TYPES_DOUBLE], &sd, 0, RW);
    addVariable(server, scratch, "Scratch/Int32", "Int32", &UA_TYPES[UA_TYPES_INT32], &si, 0, RW);

    /* Server-side permissions: AccessLevel grants the operation, the
     * per-user UserAccessLevel (see getUserAccessLevel below) does not. That is
     * the case a client cannot predict from the AccessLevel alone. */
    UA_NodeId perms = addFolder(server, "Permissions", "Permissions", root);
    UA_Double pd = 1.0;
    addVariable(server, perms, "Permissions/DeniedWrite", "DeniedWrite", &UA_TYPES[UA_TYPES_DOUBLE], &pd, 0, RW);
    addVariable(server, perms, "Permissions/DeniedRead", "DeniedRead", &UA_TYPES[UA_TYPES_DOUBLE], &pd, 0, RW);
    addVariable(server, perms, "Permissions/ReadOnly", "ReadOnly", &UA_TYPES[UA_TYPES_DOUBLE], &pd, 0, R);

    UA_NodeId large = addFolder(server, "Large", "Large", root);
    char path[64], name[32];
    for(int i = 0; i < LARGE_FOLDER_SIZE; i++) {
        snprintf(path, sizeof(path), "Large/Item%04d", i);
        snprintf(name, sizeof(name), "Item%04d", i);
        UA_Int32 v = i;
        addVariable(server, large, path, name, &UA_TYPES[UA_TYPES_INT32], &v, 0, R);
    }
}

/* --- dynamic value, historized -------------------------------------------- */

static void
rampTick(UA_Server *server, void *data) {
    rampValue += 1.0;
    UA_Variant v;
    UA_Variant_setScalar(&v, &rampValue, &UA_TYPES[UA_TYPES_DOUBLE]);
    UA_Server_writeValue(server, labId("Dynamic/Ramp"), v);
}

static void
addDynamic(UA_Server *server, UA_NodeId root, UA_HistoryDataGathering *gathering) {
    UA_NodeId folder = addFolder(server, "Dynamic", "Dynamic", root);
    UA_VariableAttributes attr = UA_VariableAttributes_default;
    attr.displayName = UA_LOCALIZEDTEXT("en", "Ramp");
    attr.dataType = UA_TYPES[UA_TYPES_DOUBLE].typeId;
    attr.accessLevel = UA_ACCESSLEVELMASK_READ | UA_ACCESSLEVELMASK_HISTORYREAD;
    attr.historizing = true;
    UA_Variant_setScalar(&attr.value, &rampValue, &UA_TYPES[UA_TYPES_DOUBLE]);
    UA_NodeId ramp;
    UA_Server_addVariableNode(server, labId("Dynamic/Ramp"), folder, UA_NS0ID(HASCOMPONENT),
                              UA_QUALIFIEDNAME(ns, "Ramp"), UA_NS0ID(BASEDATAVARIABLETYPE),
                              attr, NULL, &ramp);

    UA_HistorizingNodeIdSettings setting;
    memset(&setting, 0, sizeof(setting));
    setting.historizingBackend = UA_HistoryDataBackend_Memory(1, 2000);
    /* Small on purpose: a read of more than this many values is answered in
     * pieces behind continuation points, which a client has to follow. */
    setting.maxHistoryDataResponseSize = 25;
    setting.historizingUpdateStrategy = UA_HISTORIZINGUPDATESTRATEGY_VALUESET;
    gathering->registerNodeId(server, gathering->context, &ramp, setting);

    UA_Server_addRepeatedCallback(server, rampTick, NULL, 250, NULL);
}

/* --- methods ------------------------------------------------------------- */

static void raiseAlarm(UA_Server *server);

static UA_StatusCode
multiply(UA_Server *server, const UA_NodeId *sessionId, void *sessionHandle,
         const UA_NodeId *methodId, void *methodContext, const UA_NodeId *objectId,
         void *objectContext, size_t inputSize, const UA_Variant *input,
         size_t outputSize, UA_Variant *output) {
    UA_Double product = *(UA_Double *)input[0].data * *(UA_Double *)input[1].data;
    return UA_Variant_setScalarCopy(output, &product, &UA_TYPES[UA_TYPES_DOUBLE]);
}

static UA_StatusCode
echo(UA_Server *server, const UA_NodeId *sessionId, void *sessionHandle,
     const UA_NodeId *methodId, void *methodContext, const UA_NodeId *objectId,
     void *objectContext, size_t inputSize, const UA_Variant *input,
     size_t outputSize, UA_Variant *output) {
    UA_StatusCode rv = UA_Variant_setScalarCopy(&output[0], input[0].data, &UA_TYPES[UA_TYPES_STRING]);
    UA_UInt32 length = (UA_UInt32)((UA_String *)input[0].data)->length;
    rv |= UA_Variant_setScalarCopy(&output[1], &length, &UA_TYPES[UA_TYPES_UINT32]);
    return rv;
}

static UA_StatusCode
raiseAlarmMethod(UA_Server *server, const UA_NodeId *sessionId, void *sessionHandle,
                 const UA_NodeId *methodId, void *methodContext, const UA_NodeId *objectId,
                 void *objectContext, size_t inputSize, const UA_Variant *input,
                 size_t outputSize, UA_Variant *output) {
    raiseAlarm(server);
    return UA_STATUSCODE_GOOD;
}

static UA_Argument
argument(const char *name, const UA_DataType *type) {
    UA_Argument a;
    UA_Argument_init(&a);
    a.name = UA_STRING((char *)(uintptr_t)name);
    a.dataType = type->typeId;
    a.valueRank = UA_VALUERANK_SCALAR;
    return a;
}

static void
addMethods(UA_Server *server, UA_NodeId root) {
    UA_ObjectAttributes oattr = UA_ObjectAttributes_default;
    oattr.displayName = UA_LOCALIZEDTEXT("en", "Methods");
    UA_NodeId obj;
    UA_Server_addObjectNode(server, labId("Methods"), root, UA_NS0ID(ORGANIZES),
                            UA_QUALIFIEDNAME(ns, "Methods"), UA_NS0ID(BASEOBJECTTYPE),
                            oattr, NULL, &obj);

    UA_MethodAttributes mattr = UA_MethodAttributes_default;
    mattr.executable = true;
    mattr.userExecutable = true;

    UA_Argument mulIn[2] = {argument("a", &UA_TYPES[UA_TYPES_DOUBLE]),
                            argument("b", &UA_TYPES[UA_TYPES_DOUBLE])};
    UA_Argument mulOut[1] = {argument("product", &UA_TYPES[UA_TYPES_DOUBLE])};
    mattr.displayName = UA_LOCALIZEDTEXT("en", "Multiply");
    UA_Server_addMethodNode(server, labId("Methods/Multiply"), obj, UA_NS0ID(HASCOMPONENT),
                            UA_QUALIFIEDNAME(ns, "Multiply"), mattr, multiply,
                            2, mulIn, 1, mulOut, NULL, NULL);

    UA_Argument echoIn[1] = {argument("text", &UA_TYPES[UA_TYPES_STRING])};
    UA_Argument echoOut[2] = {argument("text", &UA_TYPES[UA_TYPES_STRING]),
                              argument("length", &UA_TYPES[UA_TYPES_UINT32])};
    mattr.displayName = UA_LOCALIZEDTEXT("en", "Echo");
    UA_Server_addMethodNode(server, labId("Methods/Echo"), obj, UA_NS0ID(HASCOMPONENT),
                            UA_QUALIFIEDNAME(ns, "Echo"), mattr, echo,
                            1, echoIn, 2, echoOut, NULL, NULL);

    /* Re-arms the lab alarm: active and unacknowledged again, so each runtime
     * in a run gets a condition of its own to acknowledge. */
    mattr.displayName = UA_LOCALIZEDTEXT("en", "RaiseAlarm");
    UA_Server_addMethodNode(server, labId("Methods/RaiseAlarm"), obj, UA_NS0ID(HASCOMPONENT),
                            UA_QUALIFIEDNAME(ns, "RaiseAlarm"), mattr, raiseAlarmMethod,
                            0, NULL, 0, NULL, NULL, NULL);

    /* Executable, but not by this user: see getUserExecutable below. */
    mattr.displayName = UA_LOCALIZEDTEXT("en", "Denied");
    UA_Server_addMethodNode(server, labId("Methods/Denied"), obj, UA_NS0ID(HASCOMPONENT),
                            UA_QUALIFIEDNAME(ns, "Denied"), mattr, multiply,
                            2, mulIn, 1, mulOut, NULL, NULL);
}

/* --- events and one retained alarm ---------------------------------------- */

static void
eventTick(UA_Server *server, void *data) {
    UA_Server_createEvent(server, UA_NS0ID(SERVER), UA_NS0ID(BASEEVENTTYPE), 500,
                          UA_LOCALIZEDTEXT("en", "Conformance lab heartbeat"), NULL, NULL,
                          NULL);
}

static void
setTwoState(UA_Server *server, const char *field, UA_Boolean id) {
    UA_Variant v;
    UA_Variant_setScalar(&v, &id, &UA_TYPES[UA_TYPES_BOOLEAN]);
    UA_StatusCode rv = UA_Server_setConditionVariableFieldProperty(
        server, alarmCondition, &v, UA_QUALIFIEDNAME(0, (char *)(uintptr_t)field),
        UA_QUALIFIEDNAME(0, "Id"));
    if(rv != UA_STATUSCODE_GOOD)
        UA_LOG_WARNING(UA_Log_Stdout, UA_LOGCATEGORY_APPLICATION, "%s/Id not set: %s", field,
                       UA_StatusCode_name(rv));
}

static void
raiseAlarm(UA_Server *server) {
    UA_Variant v;
    UA_Boolean retain = true;
    UA_Variant_setScalar(&v, &retain, &UA_TYPES[UA_TYPES_BOOLEAN]);
    UA_Server_setConditionField(server, alarmCondition, &v, UA_QUALIFIEDNAME(0, "Retain"));
    UA_UInt16 severity = 800;
    UA_Variant_setScalar(&v, &severity, &UA_TYPES[UA_TYPES_UINT16]);
    UA_Server_setConditionField(server, alarmCondition, &v, UA_QUALIFIEDNAME(0, "Severity"));
    UA_LocalizedText message = UA_LOCALIZEDTEXT("en", "Lab level high");
    UA_Variant_setScalar(&v, &message, &UA_TYPES[UA_TYPES_LOCALIZEDTEXT]);
    UA_Server_setConditionField(server, alarmCondition, &v, UA_QUALIFIEDNAME(0, "Message"));
    setTwoState(server, "EnabledState", true);
    setTwoState(server, "AckedState", false);
    setTwoState(server, "ActiveState", true);
    UA_StatusCode rv = UA_Server_triggerConditionEvent(server, alarmCondition, alarmSource, NULL);
    if(rv != UA_STATUSCODE_GOOD)
        UA_LOG_WARNING(UA_Log_Stdout, UA_LOGCATEGORY_APPLICATION,
                       "alarm not triggered: %s", UA_StatusCode_name(rv));
}

static void
addAlarm(UA_Server *server, UA_NodeId root) {
    UA_ObjectAttributes attr = UA_ObjectAttributes_default;
    attr.eventNotifier = UA_EVENTNOTIFIER_SUBSCRIBE_TO_EVENT;
    attr.displayName = UA_LOCALIZEDTEXT("en", "AlarmSource");
    UA_Server_addObjectNode(server, labId("AlarmSource"), root, UA_NS0ID(ORGANIZES),
                            UA_QUALIFIEDNAME(ns, "AlarmSource"), UA_NS0ID(BASEOBJECTTYPE),
                            attr, NULL, &alarmSource);
    UA_Server_addReference(server, UA_NS0ID(SERVER), UA_NS0ID(HASNOTIFIER),
                           UA_EXPANDEDNODEID_STRING(ns, "AlarmSource"), true);

    UA_StatusCode rv = UA_Server_createCondition(server, labId("AlarmSource/HighLevel"),
                                                 UA_NS0ID(OFFNORMALALARMTYPE),
                                                 UA_QUALIFIEDNAME(ns, "HighLevel"), alarmSource,
                                                 UA_NS0ID(HASCOMPONENT), &alarmCondition);
    if(rv != UA_STATUSCODE_GOOD) {
        UA_LOG_WARNING(UA_Log_Stdout, UA_LOGCATEGORY_APPLICATION,
                       "alarm not created: %s", UA_StatusCode_name(rv));
        return;
    }
    /* Active, unacknowledged and retained from startup, so a ConditionRefresh
     * has something to return without the harness having to cause it. */
    raiseAlarm(server);
}

/* --- access control -------------------------------------------------------- */

static UA_Byte (*defaultUserAccessLevel)(UA_Server *, UA_AccessControl *, const UA_NodeId *,
                                         void *, const UA_NodeId *, void *);
static UA_Boolean (*defaultUserExecutable)(UA_Server *, UA_AccessControl *, const UA_NodeId *,
                                           void *, const UA_NodeId *, void *);
static UA_Boolean (*defaultUserExecutableOnObject)(UA_Server *, UA_AccessControl *,
                                                   const UA_NodeId *, void *, const UA_NodeId *,
                                                   void *, const UA_NodeId *, void *);

static UA_Boolean
isLabNode(const UA_NodeId *nodeId, const char *path) {
    UA_NodeId wanted = labId(path);
    return UA_NodeId_equal(nodeId, &wanted);
}

static UA_Byte
getUserAccessLevel(UA_Server *server, UA_AccessControl *ac, const UA_NodeId *sessionId,
                   void *sessionContext, const UA_NodeId *nodeId, void *nodeContext) {
    if(isLabNode(nodeId, "Permissions/DeniedWrite"))
        return UA_ACCESSLEVELMASK_READ;
    if(isLabNode(nodeId, "Permissions/DeniedRead"))
        return 0;
    return defaultUserAccessLevel(server, ac, sessionId, sessionContext, nodeId, nodeContext);
}

static UA_Boolean
getUserExecutable(UA_Server *server, UA_AccessControl *ac, const UA_NodeId *sessionId,
                  void *sessionContext, const UA_NodeId *methodId, void *methodContext) {
    if(isLabNode(methodId, "Methods/Denied"))
        return false;
    return defaultUserExecutable(server, ac, sessionId, sessionContext, methodId, methodContext);
}

/* The Call service asks this one, per object; the one above is what a Read of
 * the UserExecutable attribute reports. Both have to say no. */
static UA_Boolean
getUserExecutableOnObject(UA_Server *server, UA_AccessControl *ac, const UA_NodeId *sessionId,
                          void *sessionContext, const UA_NodeId *methodId, void *methodContext,
                          const UA_NodeId *objectId, void *objectContext) {
    if(isLabNode(methodId, "Methods/Denied"))
        return false;
    return defaultUserExecutableOnObject(server, ac, sessionId, sessionContext, methodId,
                                         methodContext, objectId, objectContext);
}

/* --- main ---------------------------------------------------------------- */

int
main(int argc, char **argv) {
    UA_UInt16 port = 4850;
    const char *certPath = NULL, *keyPath = NULL;
    const char *trustPaths[MAX_TRUST];
    size_t trustSize = 0;
    UA_Boolean pad = false;
    for(int i = 1; i < argc; i++) {
        if(strcmp(argv[i], "--port") == 0 && i + 1 < argc)
            port = (UA_UInt16)atoi(argv[++i]);
        else if(strcmp(argv[i], "--cert") == 0 && i + 1 < argc)
            certPath = argv[++i];
        else if(strcmp(argv[i], "--key") == 0 && i + 1 < argc)
            keyPath = argv[++i];
        else if(strcmp(argv[i], "--trust") == 0 && i + 1 < argc && trustSize < MAX_TRUST)
            trustPaths[trustSize++] = argv[++i];
        else if(strcmp(argv[i], "--pad-namespace") == 0)
            pad = true;
        else {
            fprintf(stderr, "unknown argument: %s\n", argv[i]);
            return EXIT_FAILURE;
        }
    }
    if(!certPath || !keyPath) {
        fprintf(stderr, "--cert and --key are required\n");
        return EXIT_FAILURE;
    }

    UA_ByteString certificate = loadFile(certPath);
    UA_ByteString privateKey = loadFile(keyPath);
    UA_ByteString trustList[MAX_TRUST];
    for(size_t i = 0; i < trustSize; i++)
        trustList[i] = loadFile(trustPaths[i]);

    /* The whole configuration is built before the server exists: open62541
     * writes the capability nodes (AccessHistoryDataCapability and friends)
     * into namespace 0 when the server is created, so a flag set afterwards
     * is enforced but never advertised. */
    UA_ServerConfig serverConfig;
    memset(&serverConfig, 0, sizeof(serverConfig));
    UA_ServerConfig *config = &serverConfig;
    /* A trust list, not "accept every certificate": a client the list does not
     * name is refused, which is what the negative certificate scenario needs. */
    UA_StatusCode rv = UA_ServerConfig_setDefaultWithSecurityPolicies(
        config, port, &certificate, &privateKey, trustList, trustSize, NULL, 0, NULL, 0);
    if(rv != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "server config failed: %s\n", UA_StatusCode_name(rv));
        return EXIT_FAILURE;
    }
    UA_String_clear(&config->applicationDescription.applicationUri);
    config->applicationDescription.applicationUri = UA_STRING_ALLOC("urn:opcua-mcp:conformance-lab:server");

    const char *username = getenv("OPCUA_CONFORMANCE_USERNAME");
    const char *password = getenv("OPCUA_CONFORMANCE_PASSWORD");
    UA_UsernamePasswordLogin login[1];
    size_t loginSize = 0;
    if(username && password) {
        login[0].username = UA_STRING((char *)(uintptr_t)username);
        login[0].password = UA_STRING((char *)(uintptr_t)password);
        loginSize = 1;
    }
    /* Re-run after the security policies exist so the username policy is
     * offered encrypted and the X.509 policy is offered at all. */
    config->accessControl.clear(&config->accessControl);
    UA_AccessControl_default(config, true, NULL, loginSize, login);
    defaultUserAccessLevel = config->accessControl.getUserAccessLevel;
    config->accessControl.getUserAccessLevel = getUserAccessLevel;
    defaultUserExecutable = config->accessControl.getUserExecutable;
    config->accessControl.getUserExecutable = getUserExecutable;
    defaultUserExecutableOnObject = config->accessControl.getUserExecutableOnObject;
    config->accessControl.getUserExecutableOnObject = getUserExecutableOnObject;

    /* Operation limits a client has to honour rather than assume away. */
    config->maxNodesPerRead = 40;
    config->maxNodesPerWrite = 20;
    config->maxNodesPerBrowse = 50;
    config->maxReferencesPerNode = 100;

    UA_HistoryDataGathering gathering = UA_HistoryDataGathering_Default(1);
    config->historyDatabase = UA_HistoryDatabase_default(gathering);
    config->accessHistoryDataCapability = true;
    config->maxReturnDataValues = 0;

    UA_String_clear(&config->buildInfo.productName);
    config->buildInfo.productName = UA_STRING_ALLOC("open62541 conformance lab server");

    UA_Server *server = UA_Server_newWithConfig(config);
    if(!server) {
        fprintf(stderr, "server creation failed\n");
        return EXIT_FAILURE;
    }

    if(pad)
        UA_Server_addNamespace(server, "urn:opcua-mcp:conformance-lab:padding");
    ns = UA_Server_addNamespace(server, LAB_NAMESPACE);

    UA_NodeId root = addFolder(server, "Lab", "Lab", UA_NS0ID(OBJECTSFOLDER));
    addValues(server, root);
    addDynamic(server, root, &gathering);
    addMethods(server, root);
    addAlarm(server, root);
    UA_Server_addRepeatedCallback(server, eventTick, NULL, 1000, NULL);

    rv = UA_Server_runUntilInterrupt(server);
    UA_Server_delete(server);
    UA_ByteString_clear(&certificate);
    UA_ByteString_clear(&privateKey);
    for(size_t i = 0; i < trustSize; i++)
        UA_ByteString_clear(&trustList[i]);
    return rv == UA_STATUSCODE_GOOD ? EXIT_SUCCESS : EXIT_FAILURE;
}
