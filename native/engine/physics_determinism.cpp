// Offline determinism harness for the physics rewrite (synthetic, never loaded into X4).
//
// Question: X4 9.00 steps Jolt 5.5.0 with its own job system (XPhys::XJobSystem). That job system
// never hands a ready job to a worker: every job runs on the calling thread (the MoveWorker) inside
// WaitForJobs, in barrier-insertion order, while GetMaxConcurrency() reports the JobThreadManager
// worker count (logical processors - 2, clamped 4..32: 30 on a 16-core/32-thread PC), so the
// *decomposition* of the step depends on the PC. Would (a) a different decomposition and (b) true multi-threaded execution change the
// simulation? This harness runs identical scenes under
//   x4serial-cN : a re-implementation of X4's executor (serial, first-ready-job-in-insertion-order)
//                 reporting max concurrency N (4 = minimum, 6/14/22 = 4/8/12-core SMT PCs, 30 = 16-core
//                 SMT PC (reference), 32 = maximum)
//   jolt-single : Jolt's JobSystemSingleThreaded (max concurrency 1)
//   pool-T      : Jolt's JobSystemThreadPool with T worker threads (true parallel execution)
// with X4's PhysicsSettings (Jolt defaults, mAllowSleeping=false, mTimeBeforeSleep=1e4, zero gravity,
// mDeterministicSimulation=true) and compares, every frame, the complete PhysicsSystem::SaveState
// (bodies, velocities, contact cache, global state) byte for byte, plus the contact callbacks as an
// ordered sequence and as an order-independent set.
//
// Inputs are identical across configurations: the per-frame velocity overrides (X4 copies component
// velocities into bodies before every step) are applied serially, in body order, before Update.
// Build/run: python native/engine/physics_build.py --zig <zig> [--jolt <JoltPhysics-5.5.0 checkout>]
#include <Jolt/Jolt.h>
#include <Jolt/RegisterTypes.h>
#include <Jolt/Core/Factory.h>
#include <Jolt/Core/TempAllocator.h>
#include <Jolt/Core/JobSystemThreadPool.h>
#include <Jolt/Core/JobSystemSingleThreaded.h>
#include <Jolt/Physics/PhysicsSettings.h>
#include <Jolt/Physics/PhysicsSystem.h>
#include <Jolt/Physics/StateRecorderImpl.h>
#include <Jolt/Physics/Collision/ContactListener.h>
#include <Jolt/Physics/Collision/Shape/BoxShape.h>
#include <Jolt/Physics/Collision/Shape/SphereShape.h>
#include <Jolt/Physics/Collision/Shape/CapsuleShape.h>
#include <Jolt/Physics/Collision/Shape/ConvexHullShape.h>
#include <Jolt/Physics/Collision/Shape/MutableCompoundShape.h>
#include <Jolt/Physics/Collision/Shape/StaticCompoundShape.h>
#include <Jolt/Physics/Collision/Shape/MeshShape.h>
#include <Jolt/Physics/Collision/Shape/OffsetCenterOfMassShape.h>
#include <Jolt/Physics/Body/BodyCreationSettings.h>

#include <algorithm>
#include <chrono>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

JPH_SUPPRESS_WARNINGS
using namespace JPH;

static void TraceImpl(const char *inFMT, ...) {
    va_list list;
    va_start(list, inFMT);
    vfprintf(stderr, inFMT, list);
    va_end(list);
    fputc('\n', stderr);
}

// ---------------------------------------------------------------------------------------------
// X4-style executor: semantics of XPhys::XJobSystem (Windows 0xc5850 vtable 0x2a2aba0) re-implemented
// from behaviour, not from code. QueueJob ignores ready jobs, WaitForJobs scans the barrier from the
// oldest job for the first job whose dependency count is zero and executes it on the waiting thread.
class X4StyleJobSystem final : public JobSystem {
public:
    explicit X4StyleJobSystem(int inMaxConcurrency) : mMaxConcurrency(inMaxConcurrency) {}
    int GetMaxConcurrency() const override { return mMaxConcurrency; }
    JobHandle CreateJob(const char *inName, ColorArg inColor, const JobFunction &inJobFunction,
                        uint32 inNumDependencies) override {
        Job *job = new Job(inName, inColor, this, inJobFunction, inNumDependencies);
        JobHandle handle(job);
        if (inNumDependencies == 0) QueueJob(job);
        ++mJobsCreated;
        return handle;
    }
    Barrier *CreateBarrier() override { return new X4Barrier(); }
    void DestroyBarrier(Barrier *inBarrier) override { delete static_cast<X4Barrier *>(inBarrier); }
    void WaitForJobs(Barrier *inBarrier) override { static_cast<X4Barrier *>(inBarrier)->Wait(); }
    uint64_t mJobsCreated = 0;

protected:
    void QueueJob(Job *) override {}  // a ready job is never handed to a worker
    void QueueJobs(Job **inJobs, uint inNumJobs) override {
        for (uint i = 0; i < inNumJobs; ++i) QueueJob(inJobs[i]);
    }
    void FreeJob(Job *inJob) override { delete inJob; }

private:
    class X4Barrier final : public Barrier {
    public:
        void AddJob(const JobHandle &inJob) override {
            std::lock_guard<std::recursive_mutex> lock(mMutex);
            Job *job = inJob.GetPtr();
            if (job->SetBarrier(this)) {
                job->AddRef();
                mJobs.push_back(job);
            }
        }
        void AddJobs(const JobHandle *inHandles, uint inNumHandles) override {
            std::lock_guard<std::recursive_mutex> lock(mMutex);
            for (uint i = 0; i < inNumHandles; ++i) AddJob(inHandles[i]);
        }
        void Wait() {
            for (;;) {
                std::lock_guard<std::recursive_mutex> lock(mMutex);
                while (!mJobs.empty() && mJobs.front()->IsDone()) {
                    Job *job = mJobs.front();
                    mJobs.pop_front();
                    job->Release();
                }
                if (mJobs.empty()) return;
                for (Job *job : mJobs)
                    if (job->CanBeExecuted()) { job->Execute(); break; }  // OnJobFinished erases it
            }
        }

    protected:
        void OnJobFinished(Job *inJob) override {
            std::lock_guard<std::recursive_mutex> lock(mMutex);
            auto it = std::find(mJobs.begin(), mJobs.end(), inJob);
            if (it != mJobs.end()) mJobs.erase(it);
            inJob->Release();
        }

    private:
        std::recursive_mutex mMutex;
        std::deque<Job *> mJobs;
    };
    int mMaxConcurrency;
};

#ifdef _MSC_VER
// MSVC builds only (physics_build.py --msvc): the C serial barrier of physics_jobs.h ("joltwait", the
// mode the probe can switch X4 to) behind an X4-like job system. Same ABI as inside X4.
extern "C" {
typedef uint32_t (*pjm_execute_fn)(void *job);
void pjm_set_execute(pjm_execute_fn execute);
void *pjm_create_barrier(void *system);
void pjm_destroy_barrier(void *system, void *barrier);
void pjm_wait(void *system, void *barrier);
unsigned long long pjm_jobs_run(void);
unsigned long long pjm_anomalies(void);
int pjp_start(int threads, pjm_execute_fn execute);
void pjp_stop(void);
int pjp_max_concurrency(void *system);
void *pjp_create_job(void *system, void **ret, const char *name, uint32_t color, const void *function, uint32_t deps);
void pjp_queue_job(void *system, void *job);
void pjp_queue_jobs(void *system, void **jobs, uint32_t n);
void pjp_free_job(void *system, void *job);
void *pjp_create_barrier(void *system);
void pjp_destroy_barrier(void *system, void *barrier);
void pjp_wait(void *system, void *barrier);
unsigned long long pjp_stats(int which);
int pjc_init(void);
void *pjc_add(uint32_t kind, void *listener, void *body1, void *body2, const void *manifold, const void *settings);
void pjc_removed(void *listener, const void *pair);
typedef void (*pjc_contact_fn)(void *listener, void *body1, void *body2, void *manifold, void *settings);
typedef void (*pjc_removed_fn)(void *listener, const void *pair);
void pjc_replay(pjc_contact_fn added, pjc_contact_fn persisted, pjc_removed_fn removed);
unsigned long long pjc_stats(int which);
}
// X4-like job system whose every slot is the C pool of physics_pool.h (what the probe's "parallel" mode
// installs into XJobSystem's vtable).
class X4PoolJobSystem final : public JobSystem {
public:
    explicit X4PoolJobSystem(int inThreads) {
        if (!pjp_start(inThreads, &sExecute)) { fprintf(stderr, "pool start failed\n"); exit(2); }
    }
    ~X4PoolJobSystem() override { pjp_stop(); }
    int GetMaxConcurrency() const override { return pjp_max_concurrency(const_cast<X4PoolJobSystem *>(this)); }
    JobHandle CreateJob(const char *inName, ColorArg inColor, const JobFunction &inJobFunction,
                        uint32 inNumDependencies) override {
        JobHandle handle;   // a Ref<Job>: one pointer, filled by the C function (reference already taken)
        pjp_create_job(this, reinterpret_cast<void **>(&handle), inName, inColor.GetUInt32(), &inJobFunction, inNumDependencies);
        return handle;
    }
    Barrier *CreateBarrier() override { return static_cast<Barrier *>(pjp_create_barrier(this)); }
    void DestroyBarrier(Barrier *inBarrier) override { pjp_destroy_barrier(this, inBarrier); }
    void WaitForJobs(Barrier *inBarrier) override { pjp_wait(this, inBarrier); }

protected:
    void QueueJob(Job *inJob) override { pjp_queue_job(this, inJob); }
    void QueueJobs(Job **inJobs, uint inNumJobs) override { pjp_queue_jobs(this, reinterpret_cast<void **>(inJobs), inNumJobs); }
    void FreeJob(Job *inJob) override { pjp_free_job(this, inJob); }

private:
    static uint32_t sExecute(void *inJob) { return static_cast<Job *>(inJob)->Execute(); }
};
class X4CJobSystem final : public JobSystem {
public:
    explicit X4CJobSystem(int inMaxConcurrency) : mMaxConcurrency(inMaxConcurrency) { pjm_set_execute(&sExecute); }
    int GetMaxConcurrency() const override { return mMaxConcurrency; }
    JobHandle CreateJob(const char *inName, ColorArg inColor, const JobFunction &inJobFunction,
                        uint32 inNumDependencies) override {
        Job *job = new Job(inName, inColor, this, inJobFunction, inNumDependencies);
        JobHandle handle(job);
        if (inNumDependencies == 0) QueueJob(job);
        return handle;
    }
    Barrier *CreateBarrier() override { return static_cast<Barrier *>(pjm_create_barrier(this)); }
    void DestroyBarrier(Barrier *inBarrier) override { pjm_destroy_barrier(this, inBarrier); }
    void WaitForJobs(Barrier *inBarrier) override { pjm_wait(this, inBarrier); }

protected:
    void QueueJob(Job *) override {}
    void QueueJobs(Job **, uint) override {}
    void FreeJob(Job *inJob) override { delete inJob; }

private:
    static uint32_t sExecute(void *inJob) { return static_cast<Job *>(inJob)->Execute(); }
    int mMaxConcurrency;
};
#endif

// ---------------------------------------------------------------------------------------------
namespace Layers {
static constexpr ObjectLayer NON_MOVING = 0;
static constexpr ObjectLayer MOVING = 1;
static constexpr ObjectLayer NUM_LAYERS = 2;
}  // namespace Layers
class PairFilter final : public ObjectLayerPairFilter {
public:
    bool ShouldCollide(ObjectLayer a, ObjectLayer b) const override {
        return a == Layers::MOVING || b == Layers::MOVING;
    }
};
class BPInterface final : public BroadPhaseLayerInterface {
public:
    uint GetNumBroadPhaseLayers() const override { return 2; }
    BroadPhaseLayer GetBroadPhaseLayer(ObjectLayer inLayer) const override { return BroadPhaseLayer((uint8)inLayer); }
};
class ObjVsBP final : public ObjectVsBroadPhaseLayerFilter {
public:
    bool ShouldCollide(ObjectLayer inLayer, BroadPhaseLayer inBP) const override {
        return inLayer == Layers::MOVING || inBP.GetValue() == Layers::MOVING;
    }
};

struct ContactRecord {
    uint32_t kind, body1, body2, sub1, sub2;
    float px, py, pz, nx, ny, nz, depth;
    bool operator<(const ContactRecord &o) const {
        if (kind != o.kind) return kind < o.kind;
        if (body1 != o.body1) return body1 < o.body1;
        if (body2 != o.body2) return body2 < o.body2;
        if (sub1 != o.sub1) return sub1 < o.sub1;
        return sub2 < o.sub2;
    }
};

// Records every callback (thread safe; callbacks arrive from worker threads in pool configs). It also
// aggregates like X4's listener: per body pair a count and the sum of the manifold points, accumulated in
// callback order (X4: XContactListener 0xb75a0), which is where the callback order reaches state.
static uint64_t fnv1a_seed(const void *data, size_t n, uint64_t h);
struct Aggregate { float count = 0, x = 0, y = 0, z = 0; };
class RecordingListener final : public ContactListener {
public:
    bool mBuffered = false;   // MSVC builds: record into the C buffer, aggregate at the sorted replay
    void Aggregate1(const Body &a, const Body &b, const ContactManifold &m) {
        std::lock_guard<std::mutex> lock(mMutex);
        AggregateUnlocked(a.GetID().GetIndexAndSequenceNumber(), b.GetID().GetIndexAndSequenceNumber(), m);
    }
    void AggregateUnlocked(uint32_t a, uint32_t b, const ContactManifold &m) {
        ::Aggregate &g = mAggregates[((uint64_t)a << 32) | b];
        float sx = 0, sy = 0, sz = 0;
        for (uint i = 0; i < m.mRelativeContactPointsOn1.size(); ++i) {
            sx += m.mRelativeContactPointsOn1[i].GetX();
            sy += m.mRelativeContactPointsOn1[i].GetY();
            sz += m.mRelativeContactPointsOn1[i].GetZ();
        }
        float n = (float)m.mRelativeContactPointsOn1.size();
        if (n > 0) { sx /= n; sy /= n; sz /= n; }
        g.count += 1.0f; g.x += sx; g.y += sy; g.z += sz;
    }
    uint64_t TakeAggregateHash() {
        std::lock_guard<std::mutex> lock(mMutex);
        uint64_t h = 1469598103934665603ull;
        for (auto &kv : mAggregates) {
            h = fnv1a_seed(&kv.first, sizeof(kv.first), h);
            h = fnv1a_seed(&kv.second, sizeof(kv.second), h);
        }
        mAggregates.clear();
        return h;
    }
    std::map<uint64_t, ::Aggregate> mAggregates;
    ValidateResult OnContactValidate(const Body &, const Body &, RVec3Arg, const CollideShapeResult &) override {
        return ValidateResult::AcceptAllContactsForThisBodyPair;  // what X4's listener returns
    }
    void OnContactAdded(const Body &a, const Body &b, const ContactManifold &m, ContactSettings &s) override { dispatch(0, a, b, m, s); }
    void OnContactPersisted(const Body &a, const Body &b, const ContactManifold &m, ContactSettings &s) override { dispatch(1, a, b, m, s); }
    void dispatch(uint32_t kind, const Body &a, const Body &b, const ContactManifold &m, ContactSettings &s) {
#ifdef _MSC_VER
        if (mBuffered) { pjc_add(kind, this, (void *)&a, (void *)&b, &m, &s); return; }
#endif
        (void)s;
        add(kind, a, b, m);
        Aggregate1(a, b, m);
    }
    void OnContactRemoved(const SubShapeIDPair &p) override {
#ifdef _MSC_VER
        if (mBuffered) { pjc_removed(this, &p); return; }
#endif
        RecordRemoved(p);
    }
    void RecordRemoved(const SubShapeIDPair &p) {
        ContactRecord r{2, p.GetBody1ID().GetIndexAndSequenceNumber(), p.GetBody2ID().GetIndexAndSequenceNumber(),
                        p.GetSubShapeID1().GetValue(), p.GetSubShapeID2().GetValue(), 0, 0, 0, 0, 0, 0, 0};
        std::lock_guard<std::mutex> lock(mMutex);
        mRecords.push_back(r);
    }
    std::vector<ContactRecord> Take() {
        std::lock_guard<std::mutex> lock(mMutex);
        std::vector<ContactRecord> out;
        out.swap(mRecords);
        return out;
    }

    void add(uint32_t kind, const Body &a, const Body &b, const ContactManifold &m) {
        RVec3 p = m.GetWorldSpaceContactPointOn1(0);
        ContactRecord r{kind, a.GetID().GetIndexAndSequenceNumber(), b.GetID().GetIndexAndSequenceNumber(),
                        m.mSubShapeID1.GetValue(), m.mSubShapeID2.GetValue(),
                        (float)p.GetX(), (float)p.GetY(), (float)p.GetZ(),
                        m.mWorldSpaceNormal.GetX(), m.mWorldSpaceNormal.GetY(), m.mWorldSpaceNormal.GetZ(), m.mPenetrationDepth};
        std::lock_guard<std::mutex> lock(mMutex);
        mRecords.push_back(r);
    }
private:
    std::mutex mMutex;
    std::vector<ContactRecord> mRecords;
};

static uint64_t fnv1a_seed(const void *data, size_t n, uint64_t h) {
    const unsigned char *p = (const unsigned char *)data;
    for (size_t i = 0; i < n; ++i) { h ^= p[i]; h *= 1099511628211ull; }
    return h;
}
static uint64_t fnv1a(const void *data, size_t n, uint64_t h = 1469598103934665603ull) {
    const unsigned char *p = (const unsigned char *)data;
    for (size_t i = 0; i < n; ++i) { h ^= p[i]; h *= 1099511628211ull; }
    return h;
}

// Deterministic generator for the scene and the per-frame inputs (same in every configuration).
struct Rng {
    uint64_t s;
    explicit Rng(uint64_t seed) : s(seed * 0x9e3779b97f4a7c15ull + 1) {}
    uint32_t next() { s = s * 6364136223846793005ull + 1442695040888963407ull; return (uint32_t)(s >> 33); }
    float uniform(float lo, float hi) { return lo + (hi - lo) * (float)(next() & 0xffffff) / (float)0x1000000; }
};

struct Scenario {
    const char *name;
    int frames;
    int collisionSteps;
    int dynamicBodies;
    float extent;       // half size of the spawn cube (m)
    float speed;        // max speed (m/s)
    float overrideFrac; // share of bodies whose velocity is overridden every frame (X4 pre-sync)
    bool packed;        // dense cluster (large islands)
    bool fast;          // add LinearCast projectiles-sized bodies
};

static RefConst<Shape> make_hull(Rng &r, float size) {
    Array<Vec3> pts;
    for (int i = 0; i < 12; ++i) pts.push_back(Vec3(r.uniform(-size, size), r.uniform(-size * 0.5f, size * 0.5f), r.uniform(-size * 1.5f, size * 1.5f)));
    ConvexHullShapeSettings s(pts, 0.05f);
    return s.Create().Get();
}

static RefConst<Shape> make_ship_shape(Rng &r, int kind) {
    float size = r.uniform(2.0f, 12.0f);
    switch (kind % 6) {
    case 0: return new BoxShape(Vec3(size, size * 0.4f, size * 1.6f));
    case 1: return new SphereShape(size);
    case 2: return new CapsuleShape(size, size * 0.5f);
    case 3: return make_hull(r, size);
    case 4: {  // X4 builds ships as MutableCompoundShape of wrapped parts
        MutableCompoundShapeSettings s;
        int parts = 2 + (int)(r.next() % 3);
        for (int i = 0; i < parts; ++i)
            s.AddShape(Vec3(r.uniform(-size, size), r.uniform(-size, size), r.uniform(-size, size)),
                       Quat::sRotation(Vec3::sAxisY(), r.uniform(0.0f, 3.0f)), make_hull(r, size * 0.6f));
        return s.Create().Get();
    }
    default: {
        OffsetCenterOfMassShapeSettings s(Vec3(r.uniform(-1, 1), r.uniform(-1, 1), r.uniform(-1, 1)), make_hull(r, size));
        return s.Create().Get();
    }
    }
}

static RefConst<Shape> make_station(Rng &r) {
    StaticCompoundShapeSettings s;
    for (int i = 0; i < 24; ++i)
        s.AddShape(Vec3(r.uniform(-60, 60), r.uniform(-20, 20), r.uniform(-60, 60)), Quat::sIdentity(),
                   new BoxShape(Vec3(r.uniform(4, 15), r.uniform(4, 15), r.uniform(4, 15))));
    return s.Create().Get();
}

static RefConst<Shape> make_asteroid(Rng &r) {
    // Irregular closed triangle mesh (icosphere-like with noise)
    VertexList v;
    IndexedTriangleList t;
    const int rings = 10, segs = 16;
    float radius = r.uniform(30, 60);
    for (int i = 0; i <= rings; ++i)
        for (int j = 0; j < segs; ++j) {
            float th = 3.14159265f * i / rings, ph = 6.2831853f * j / segs;
            float rr = radius * r.uniform(0.85f, 1.15f);
            v.push_back(Float3(rr * sinf(th) * cosf(ph), rr * cosf(th), rr * sinf(th) * sinf(ph)));
        }
    for (int i = 0; i < rings; ++i)
        for (int j = 0; j < segs; ++j) {
            uint32 a = i * segs + j, b = i * segs + (j + 1) % segs, c = (i + 1) * segs + j, d = (i + 1) * segs + (j + 1) % segs;
            t.push_back(IndexedTriangle(a, c, b));
            t.push_back(IndexedTriangle(b, c, d));
        }
    MeshShapeSettings s(v, t);
    return s.Create().Get();
}

#ifdef _MSC_VER
static void ReplayAdded(void *listener, void *a, void *b, void *m, void *s) {
    RecordingListener *l = static_cast<RecordingListener *>(listener);
    (void)s;
    l->add(0, *(const Body *)a, *(const Body *)b, *(const ContactManifold *)m);
    l->Aggregate1(*(const Body *)a, *(const Body *)b, *(const ContactManifold *)m);
}
static void ReplayPersisted(void *listener, void *a, void *b, void *m, void *s) {
    RecordingListener *l = static_cast<RecordingListener *>(listener);
    (void)s;
    l->add(1, *(const Body *)a, *(const Body *)b, *(const ContactManifold *)m);
    l->Aggregate1(*(const Body *)a, *(const Body *)b, *(const ContactManifold *)m);
}
static void ReplayRemoved(void *listener, const void *pair) {
    static_cast<RecordingListener *>(listener)->RecordRemoved(*(const SubShapeIDPair *)pair);
}
#endif

struct World {
    std::unique_ptr<PhysicsSystem> sys;
    BPInterface bp;
    ObjVsBP ovb;
    PairFilter pf;
    RecordingListener listener;
    std::vector<BodyID> dynamic;
    std::vector<Vec3> baseVel;
};

static void build_world(World &w, const Scenario &sc) {
    w.sys = std::make_unique<PhysicsSystem>();
    w.sys->Init(4096, 0, 16384, 16384, w.bp, w.ovb, w.pf);
    PhysicsSettings ps;  // X4: Jolt defaults except sleeping (XPhys::Scene::Init 0xc6890)
    ps.mTimeBeforeSleep = 10000.0f;
    ps.mAllowSleeping = false;
    w.sys->SetPhysicsSettings(ps);
    w.sys->SetGravity(Vec3::sZero());
    w.sys->SetContactListener(&w.listener);
    BodyInterface &bi = w.sys->GetBodyInterface();
    Rng r(1234 + (uint64_t)sc.dynamicBodies);
    // Static scenery
    for (int i = 0; i < 6; ++i) {
        BodyCreationSettings s(i % 2 ? make_station(r) : make_asteroid(r),
                               RVec3(r.uniform(-sc.extent, sc.extent), r.uniform(-sc.extent, sc.extent), r.uniform(-sc.extent, sc.extent)),
                               Quat::sIdentity(), EMotionType::Static, Layers::NON_MOVING);
        bi.CreateAndAddBody(s, EActivation::DontActivate);
    }
    for (int i = 0; i < sc.dynamicBodies; ++i) {
        RVec3 pos = sc.packed ? RVec3(r.uniform(-1, 1) * sc.extent * 0.3f, r.uniform(-1, 1) * sc.extent * 0.3f, r.uniform(-1, 1) * sc.extent * 0.3f)
                              : RVec3(r.uniform(-sc.extent, sc.extent), r.uniform(-sc.extent, sc.extent), r.uniform(-sc.extent, sc.extent));
        BodyCreationSettings s(make_ship_shape(r, i), pos, Quat::sRotation(Vec3(r.uniform(-1, 1), r.uniform(-1, 1), r.uniform(0.1f, 1)).Normalized(), r.uniform(0, 6)),
                               EMotionType::Dynamic, Layers::MOVING);
        s.mLinearDamping = 0.0f;
        s.mAngularDamping = 0.0f;
        if (sc.fast && i % 4 == 0) s.mMotionQuality = EMotionQuality::LinearCast;
        Vec3 toCenter = -Vec3((float)pos.GetX(), (float)pos.GetY(), (float)pos.GetZ());
        float len = toCenter.Length();
        Vec3 v = (len > 1e-3f ? toCenter / len : Vec3::sAxisX()) * r.uniform(0.2f, 1.0f) * sc.speed +
                 Vec3(r.uniform(-1, 1), r.uniform(-1, 1), r.uniform(-1, 1)) * (0.2f * sc.speed);
        if (sc.fast && i % 4 == 0) v *= 4.0f;
        s.mLinearVelocity = v;
        s.mAngularVelocity = Vec3(r.uniform(-1, 1), r.uniform(-1, 1), r.uniform(-1, 1));
        BodyID id = bi.CreateAndAddBody(s, EActivation::Activate);
        w.dynamic.push_back(id);
        w.baseVel.push_back(v);
    }
    w.sys->OptimizeBroadPhase();
}

struct RunResult {
    std::vector<uint64_t> stateHash, contactSeqHash, contactSetHash, aggregateHash;
    std::vector<size_t> contacts;
    std::string firstFrameState, lastState;
    double stepUs = 0.0;
    uint64_t jobs = 0;
};

enum class Exec { X4Serial, JoltSingle, Pool, X4C, X4Pool };

static RunResult run(const Scenario &sc, Exec exec, int param, bool buffered = false) {
    World w;
    build_world(w, sc);
    w.listener.mBuffered = buffered;
    std::unique_ptr<JobSystem> js;
    X4StyleJobSystem *x4 = nullptr;
    if (exec == Exec::X4Serial) { x4 = new X4StyleJobSystem(param); js.reset(x4); }
    else if (exec == Exec::JoltSingle) js.reset(new JobSystemSingleThreaded(cMaxPhysicsJobs));
#ifdef _MSC_VER
    else if (exec == Exec::X4C) js.reset(new X4CJobSystem(param));
    else if (exec == Exec::X4Pool) js.reset(new X4PoolJobSystem(param));
#endif
    else js.reset(new JobSystemThreadPool(cMaxPhysicsJobs, cMaxPhysicsBarriers, param));
    TempAllocatorImpl temp(256 * 1024 * 1024);
    BodyInterface &bi = w.sys->GetBodyInterface();
    RunResult out;
    const float dt = (float)sc.collisionSteps / 60.0f;
    Rng inputs(99);
    double totalUs = 0.0;
    for (int f = 0; f < sc.frames; ++f) {
        // X4 pre-sync: velocities copied from the components into a subset of bodies, serially.
        for (size_t i = 0; i < w.dynamic.size(); ++i) {
            uint32_t roll = inputs.next();
            if ((roll & 0xffff) < (uint32_t)(sc.overrideFrac * 65536.0f)) {
                float phase = 0.05f * (float)f + 0.37f * (float)i;
                Vec3 v = w.baseVel[i] * (0.75f + 0.25f * sinf(phase));
                bi.SetLinearAndAngularVelocity(w.dynamic[i], v, Vec3(0.1f * cosf(phase), 0.2f * sinf(phase), 0.0f));
            }
        }
        auto t0 = std::chrono::steady_clock::now();
        EPhysicsUpdateError err = w.sys->Update(dt, sc.collisionSteps, &temp, js.get());
        auto t1 = std::chrono::steady_clock::now();
        totalUs += std::chrono::duration<double, std::micro>(t1 - t0).count();
        if (err != EPhysicsUpdateError::None) fprintf(stderr, "WARN update error %u frame %d\n", (unsigned)err, f);
#ifdef _MSC_VER
        if (buffered) pjc_replay(&ReplayAdded, &ReplayPersisted, &ReplayRemoved);
#endif
        out.aggregateHash.push_back(w.listener.TakeAggregateHash());
        StateRecorderImpl rec;
        w.sys->SaveState(rec, EStateRecorderState::All);
        std::string data = rec.GetData();
        out.stateHash.push_back(fnv1a(data.data(), data.size()));
        if (f == 0) out.firstFrameState = data;
        if (f == sc.frames - 1) out.lastState = data;
        std::vector<ContactRecord> cs = w.listener.Take();
        out.contactSeqHash.push_back(fnv1a(cs.data(), cs.size() * sizeof(ContactRecord)));
        std::stable_sort(cs.begin(), cs.end());
        out.contactSetHash.push_back(fnv1a(cs.data(), cs.size() * sizeof(ContactRecord)));
        out.contacts.push_back(cs.size());
    }
    out.stepUs = totalUs / sc.frames;
    if (x4) out.jobs = x4->mJobsCreated;
    return out;
}

static int compare(const RunResult &ref, const RunResult &r, const std::vector<uint64_t> RunResult::*field, int *firstDiff) {
    int match = 0;
    *firstDiff = -1;
    for (size_t i = 0; i < (ref.*field).size() && i < (r.*field).size(); ++i) {
        if ((ref.*field)[i] == (r.*field)[i]) ++match;
        else if (*firstDiff < 0) *firstDiff = (int)i;
    }
    return match;
}

int main(int argc, char **argv) {
    int poolRepeats = 2;
    bool quick = false;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--quick")) quick = true;
        else if (!strncmp(argv[i], "--repeats=", 10)) poolRepeats = atoi(argv[i] + 10);
    }
    RegisterDefaultAllocator();
    Trace = TraceImpl;
    Factory::sInstance = new Factory();
    RegisterTypes();
#ifdef _MSC_VER
    if (!pjc_init()) { fputs("contact buffer allocation failed\n", stderr); return 2; }
#endif

    Scenario scenarios[] = {
        {"swarm", quick ? 120 : 600, 1, 400, 250.0f, 60.0f, 0.4f, false, false},
        {"swarm-catchup4", quick ? 60 : 300, 4, 400, 250.0f, 60.0f, 0.4f, false, false},
        {"packed-islands", quick ? 120 : 400, 1, 300, 120.0f, 15.0f, 0.2f, true, false},
        {"ccd-fast", quick ? 120 : 400, 1, 200, 200.0f, 80.0f, 0.3f, false, true},
    };
    unsigned hw = std::thread::hardware_concurrency();
    std::vector<int> pools = {1, 2, 4, 8, 15};
    if (hw >= 32) pools.push_back(31);
    int failures = 0;
    for (const Scenario &sc : scenarios) {
        // Reference: what X4 does on a 16-core/32-thread PC (30 JobThreadManager workers).
        RunResult ref = run(sc, Exec::X4Serial, 30);
        size_t maxContacts = 0, sumContacts = 0;
        for (size_t c : ref.contacts) { maxContacts = std::max(maxContacts, c); sumContacts += c; }
        printf("SCENARIO %s frames=%d collisionSteps=%d bodies=%d contactCallbacksMean=%.1f max=%zu stateBytes=%zu\n",
               sc.name, sc.frames, sc.collisionSteps, sc.dynamicBodies, (double)sumContacts / sc.frames, maxContacts, ref.lastState.size());
        printf("  config=x4serial-c30 (reference) stepUs=%.1f jobs/frame=%.1f\n", ref.stepUs, (double)ref.jobs / sc.frames);
        struct Cfg { const char *label; Exec e; int p; int repeats; bool buffered = false; };
        std::vector<Cfg> cfgs = {{"x4serial-c30-again", Exec::X4Serial, 30, 1}, {"x4serial-c4", Exec::X4Serial, 4, 1},
                                 {"x4serial-c6", Exec::X4Serial, 6, 1}, {"x4serial-c14", Exec::X4Serial, 14, 1},
                                 {"x4serial-c22", Exec::X4Serial, 22, 1}, {"x4serial-c32", Exec::X4Serial, 32, 1},
                                 {"jolt-single", Exec::JoltSingle, 0, 1}};
#ifdef _MSC_VER
        cfgs.insert(cfgs.begin(), Cfg{"x4c-c30", Exec::X4C, 30, 1});
        cfgs.insert(cfgs.begin() + 1, Cfg{"x4c-c4", Exec::X4C, 4, 1});
        // Buffered listener + sorted replay: compared with the buffered serial reference below.
        cfgs.push_back(Cfg{"BUF x4serial-c30", Exec::X4Serial, 30, 1, true});
        cfgs.push_back(Cfg{"BUF x4serial-c4", Exec::X4Serial, 4, 1, true});
        cfgs.push_back(Cfg{"BUF x4pool-1", Exec::X4Pool, 1, poolRepeats, true});
        cfgs.push_back(Cfg{"BUF x4pool-2", Exec::X4Pool, 2, poolRepeats, true});
        cfgs.push_back(Cfg{"BUF x4pool-4", Exec::X4Pool, 4, poolRepeats, true});
        cfgs.push_back(Cfg{"BUF x4pool-8", Exec::X4Pool, 8, poolRepeats, true});
        cfgs.push_back(Cfg{"BUF pool-4", Exec::Pool, 4, poolRepeats, true});
        cfgs.push_back(Cfg{"x4pool-4 (direct)", Exec::X4Pool, 4, poolRepeats});
#endif
        static char labels[16][32];
        int li = 0;
        for (int t : pools) { snprintf(labels[li], sizeof(labels[li]), "pool-%d", t); cfgs.push_back({labels[li], Exec::Pool, t, poolRepeats}); ++li; }
        RunResult bufref;   // buffered serial reference (first BUF config)
        for (const Cfg &c : cfgs) {
            for (int rep = 0; rep < c.repeats; ++rep) {
                RunResult r = run(sc, c.e, c.p, c.buffered);
                if (c.buffered && bufref.stateHash.empty()) bufref = r;
                int fdState, fdSet, fdSeq, fdAgg, fdAggRef;
                int mState = compare(ref, r, &RunResult::stateHash, &fdState);
                int mSet = compare(ref, r, &RunResult::contactSetHash, &fdSet);
                int mSeq = compare(ref, r, &RunResult::contactSeqHash, &fdSeq);
                int mAggRef = compare(ref, r, &RunResult::aggregateHash, &fdAggRef);
                int mAgg = c.buffered ? compare(bufref, r, &RunResult::aggregateHash, &fdAgg) : mAggRef;
                // State and the callback set must match; with the buffer the aggregate must match the
                // buffered reference (bit-identical listener state for every thread count).
                bool ok = mState == sc.frames && mSet == sc.frames && (!c.buffered || mAgg == sc.frames);
                if (!ok) ++failures;
                printf("  config=%s run=%d stepUs=%.1f state=%d/%d firstDiff=%d contactSet=%d/%d contactOrder=%d/%d aggregate=%d/%d%s%s\n",
                       c.label, rep + 1, r.stepUs, mState, sc.frames, fdState, mSet, sc.frames, mSeq, sc.frames, mAgg, sc.frames,
                       c.buffered ? " (vs buffered reference)" : "", ok ? "" : "  MISMATCH");
                if (c.buffered && &c == &cfgs[0]) {}
                if (c.buffered && rep == 0 && !strcmp(c.label, "BUF x4serial-c30"))
                    printf("    buffered aggregate vs unbuffered original: %d/%d frames equal\n", mAggRef, sc.frames);
            }
        }
    }
#ifdef _MSC_VER
    printf("C serial barrier: %llu jobs run, %llu anomalies\n", pjm_jobs_run(), pjm_anomalies());
    printf("C pool: %llu jobs on workers, %llu on the waiter, %llu inline, %llu alloc failures; contacts replayed %llu, lost %llu\n",
           pjp_stats(0), pjp_stats(1), pjp_stats(2), pjp_stats(3), pjc_stats(0), pjc_stats(1));
#endif
    printf("VERDICT %s (state = PhysicsSystem::SaveState(All) bytes per frame; contactSet = sorted callback records)\n",
           failures ? "MISMATCH" : "IDENTICAL");
    UnregisterTypes();
    delete Factory::sInstance;
    Factory::sInstance = nullptr;
    return failures ? 1 : 0;
}
