// te_cli: closed-loop Tennessee Eastman simulation with scheduled process
// disturbances, setpoint changes, instrument (sensor) faults and attacks.
//
// Plant model and decentralized controller (Ricker, 1996) come from the NIST
// TESIM C++ port (public domain, see te/NIST_LICENSE.md). This driver adds:
//   * scheduled IDV disturbances and setpoint changes
//   * a sensor-fault layer between the plant and the controller, so a faulty
//     transmitter is seen by the control system exactly as in a real plant
//   * replay and actuator attacks
//   * logging of both the measured (what the DCS sees) and true values
//   * an interactive mode for live use (commands on stdin, rows on stdout)
//
// Batch usage:
//   te_cli --hours H --out FILE.csv [--seed N] [--tstep h] [--log-every h]
//          [--idv K@T]...                 IDV(K) on from hour T
//          [--sp NAME@T=V]...             setpoint change at hour T
//          [--sf TAG:TYPE@T=P]...         sensor fault on XMEAS(TAG) from hour T
//          [--replay TAGS@T0:R[:J]]       replay attack: from hour T0 the listed
//                                         XMEAS tags (comma separated) replay the
//                                         readings of the previous R hours in a
//                                         loop; J = relative noise added by the
//                                         attacker to hide the repetition
//          [--act K@T=D]...               actuator attack: from hour T the real
//                                         valve K receives the controller output
//                                         + D %, while the DCS still logs the
//                                         controller output
// Interactive usage:
//   te_cli --interactive [--seed N]
//   prints the CSV header, then answers one command per line:
//     step N                    simulate N logging intervals, print N rows, then "OK"
//     sp NAME VALUE             change a setpoint now
//     idv K 0|1                 switch process disturbance K off / on
//     sf TAG TYPE P             start a sensor fault on XMEAS(TAG) now
//     sfclear TAG|all           remove sensor faults
//     act K D                   actuator attack on valve K (+D %) from now
//     actclear                  remove actuator attacks
//     replay TAGS R J           replay the listed tags' last R hours from now
//     replayclear               stop the replay
//     quit
//   Every command answers "OK", "ERR <reason>" or, once the plant's
//   interlocks have tripped, "TRIP <time> <message>".
// Sensor fault types:
//   bias   constant offset P (engineering units)
//   drift  ramp of P units per hour
//   stuck  value frozen at its last good reading (P ignored)
//   noise  extra white noise with standard deviation P
//   gain   multiplicative error: reading * (1 + P)
// Setpoint names: prod_rate, reactor_pressure, reactor_level, reactor_temp,
//                 pctg, sep_level, strip_level

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "te/TEController.h"
#include "te/TEPlant.h"
#include "te/teprob.h"

struct IdvEvent { int idx; double t; };
struct SpEvent { std::string name; double t; double value; bool done; };
struct SensorFault { int tag; std::string type; double t; double p; bool active; double frozen; };
struct ActAttack { int idx; double t; double d; };

// NY (41 measurements) and NU (12 valves) come from te/teprob.h.

static void die(const std::string& msg) {
    std::cerr << "te_cli: " << msg << std::endl;
    std::exit(2);
}

static bool valid_sp(const std::string& n) {
    return n == "prod_rate" || n == "reactor_pressure" || n == "reactor_level" || n == "reactor_temp" ||
           n == "pctg" || n == "sep_level" || n == "strip_level";
}

static void apply_setpoint(TEController* c, const std::string& n, double v) {
    if (n == "prod_rate") c->prod_rate_sp(v);
    else if (n == "reactor_pressure") c->reactor_pressure_sp(v);
    else if (n == "reactor_level") c->reactor_level_sp(v);
    else if (n == "reactor_temp") c->reactor_temp_sp(v);
    else if (n == "pctg") c->pctg_sp(v);
    else if (n == "sep_level") c->sep_level_sp(v);
    else if (n == "strip_level") c->strip_level_sp(v);
}

static bool valid_fault(const std::string& type) {
    return type == "bias" || type == "drift" || type == "stuck" || type == "noise" || type == "gain";
}

// Replay attack state. Readings are recorded into a ring buffer while no
// replay is running; a replay started at step s0 plays back the steps
// [s0 - n_rec, s0) in a loop.
struct Replay {
    std::vector<int> tags;
    double t0 = -1.0, rec_h = 0.0, jitter = 0.0;
    long n_rec = 0, s0 = -1;
    bool active = false;
};

class Sim {
public:
    double tstep = 0.0001, tscan = 0.0005, log_every = 0.05;
    std::vector<IdvEvent> idvs;
    std::vector<SpEvent> sps;
    std::vector<SensorFault> sfs;
    std::vector<ActAttack> acts;
    Replay rp;
    int shutdown = 0;
    std::string shutdown_msg;
    double t = 0.0;

    void init(unsigned long seed, double max_record_h) {
        // Plant random generator: odd 32-bit seed gives the full LCG period.
        if (seed > 0) te_seed_override = (double)((2ULL * seed * 2654435761ULL + 1ULL) % 4294967296ULL);
        rng.seed(seed + 12345);
        plant = TEPlant::getInstance();
        ctlr = TEController::getInstance();
        ctlr->initialize(tscan);
        xmv = (double*)ctlr->get_xmv();
        plant->initialize();
        steps_per_scan = std::max(1L, (long)std::llround(tscan / tstep));
        scans_per_log = std::max(1L, (long)std::llround(log_every / tscan));
        cap = std::max(1L, (long)std::llround(max_record_h / tstep));
        ring.assign((size_t)cap * NY, 0.0);
    }

    static void header(std::ostream& f) {
        f << "time";
        for (int j = 1; j <= NU; ++j) f << ",xmv_" << j;
        for (int j = 1; j <= NY; ++j) f << ",xmeas_" << j;
        for (int j = 1; j <= NY; ++j) f << ",true_" << j;
        f << ",idv_mask,sf_active\n";
    }

    // One plant integration step. Returns true when a row was logged.
    bool tick(std::ostream& f) {
        t = (double)ii * tstep;
        for (auto& e : idvs) if (t >= e.t) plant->idv(e.idx, 1);
        for (auto& e : sps) if (!e.done && t >= e.t) { apply_setpoint(ctlr, e.name, e.value); e.done = true; }
        if (!rp.tags.empty() && !rp.active && rp.t0 >= 0 && t >= rp.t0) start_replay();

        // Actuator attack: the real valve differs from the logged command.
        double plant_xmv[NU];
        std::memcpy(plant_xmv, xmv, sizeof(plant_xmv));
        for (auto& a : acts)
            if (t >= a.t) plant_xmv[a.idx] = std::min(100.0, std::max(0.0, plant_xmv[a.idx] + a.d));

        double* x;
        try {
            x = plant->increment(t, tstep, plant_xmv, &shutdown);
        } catch (TEPlant::ShutdownException& e) {
            shutdown = e.m_sd_code;
            shutdown_msg = e.m_sd_msg ? e.m_sd_msg : "";
            return false;
        }
        std::memcpy(truth, x, sizeof(truth));
        std::memcpy(meas, x, sizeof(meas));

        // Instrument fault layer: what the transmitter reports to the DCS.
        bool any = false;
        for (auto& s : sfs) {
            if (t < s.t) { s.frozen = truth[s.tag]; continue; }
            s.active = true; any = true;
            double& m = meas[s.tag];
            if (s.type == "bias") m += s.p;
            else if (s.type == "drift") m += s.p * (t - s.t);
            else if (s.type == "stuck") m = s.frozen;
            else if (s.type == "noise") m += s.p * gauss(rng);
            else if (s.type == "gain") m *= (1.0 + s.p);
        }

        // Replay attack: record readings, or play recorded ones back.
        if (!rp.active) {
            std::memcpy(&ring[(size_t)(ii % cap) * NY], meas, sizeof(meas));
        } else {
            long r = rp.s0 - rp.n_rec + ((ii - (rp.s0 - rp.n_rec)) % rp.n_rec);
            const double* row = &ring[(size_t)(r % cap) * NY];
            for (int tag : rp.tags) {
                double v = row[tag];
                if (rp.jitter > 0) v *= 1.0 + rp.jitter * gauss(rng);
                meas[tag] = v;
            }
            any = true;
        }

        bool logged = false;
        if (ii % steps_per_scan == 0) {
            xmv = ctlr->increment(t, tscan, meas);
            if (scan_count % scans_per_log == 0) {
                f << t;
                for (int j = 0; j < NU; ++j) f << "," << xmv[j];
                for (int j = 0; j < NY; ++j) f << "," << meas[j];
                for (int j = 0; j < NY; ++j) f << "," << truth[j];
                unsigned mask = 0;
                for (int k = 0; k < 20; ++k) mask |= (plant->get_idv(k) ? 1u : 0u) << k;
                f << "," << mask << "," << (any ? 1 : 0) << "\n";
                logged = true;
            }
            ++scan_count;
        }
        ++ii;
        return logged;
    }

    // Start replaying the readings of the last rec_h hours from the current step.
    bool start_replay() {
        rp.n_rec = std::max(1L, (long)std::llround(rp.rec_h / tstep));
        if (rp.n_rec > cap || rp.n_rec > ii) return false;
        rp.s0 = ii;
        rp.active = true;
        return true;
    }

    double truth_of(int tag) const { return truth[tag]; }

    TEPlant* plant = nullptr;
    TEController* ctlr = nullptr;

private:
    double* xmv = nullptr;
    double meas[NY] = {0}, truth[NY] = {0};
    long ii = 0, scan_count = 0, steps_per_scan = 1, scans_per_log = 1, cap = 1;
    std::vector<double> ring;
    std::mt19937_64 rng;
    std::normal_distribution<double> gauss{0.0, 1.0};
};

static std::vector<int> parse_tags(const std::string& tags) {
    std::vector<int> out;
    size_t pos = 0;
    while (pos < tags.size()) {
        size_t c = tags.find(',', pos);
        if (c == std::string::npos) c = tags.size();
        int tag = std::atoi(tags.substr(pos, c - pos).c_str());
        if (tag < 1 || tag > NY) return {};
        out.push_back(tag - 1);
        pos = c + 1;
    }
    return out;
}

// ---------------------------------------------------------------------------
// Interactive mode
// ---------------------------------------------------------------------------
static int interactive(Sim& sim) {
    std::ostream& out = std::cout;
    out.precision(8);
    Sim::header(out);
    out.flush();
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd.empty()) continue;
        if (cmd == "quit") break;
        if (sim.shutdown) {
            out << "TRIP " << sim.t << " " << sim.shutdown_msg << "\n" << std::flush;
            continue;
        }
        std::string err;
        if (cmd == "step") {
            long n = 0;
            in >> n;
            if (n < 1 || n > 10000) err = "step count must be 1..10000";
            for (long k = 0; err.empty() && k < n && !sim.shutdown;) {
                if (sim.tick(out)) ++k;
            }
            if (sim.shutdown) {
                out << "TRIP " << sim.t << " " << sim.shutdown_msg << "\n" << std::flush;
                continue;
            }
        } else if (cmd == "sp") {
            std::string name; double v;
            if (!(in >> name >> v) || !valid_sp(name)) err = "usage: sp NAME VALUE";
            else apply_setpoint(sim.ctlr, name, v);
        } else if (cmd == "idv") {
            int k, on;
            if (!(in >> k >> on) || k < 1 || k > 20) err = "usage: idv K 0|1";
            else {
                sim.idvs.erase(std::remove_if(sim.idvs.begin(), sim.idvs.end(),
                               [&](const IdvEvent& e) { return e.idx == k - 1; }), sim.idvs.end());
                sim.plant->idv(k - 1, on ? 1 : 0);
            }
        } else if (cmd == "sf") {
            int tag; std::string type; double p = 0.0;
            if (!(in >> tag >> type) || tag < 1 || tag > NY || !valid_fault(type)) err = "usage: sf TAG TYPE P";
            else {
                in >> p;
                sim.sfs.push_back({tag - 1, type, sim.t, p, false, sim.truth_of(tag - 1)});
            }
        } else if (cmd == "sfclear") {
            std::string which;
            in >> which;
            if (which == "all") sim.sfs.clear();
            else {
                int tag = std::atoi(which.c_str()) - 1;
                sim.sfs.erase(std::remove_if(sim.sfs.begin(), sim.sfs.end(),
                              [&](const SensorFault& s) { return s.tag == tag; }), sim.sfs.end());
            }
        } else if (cmd == "act") {
            int k; double d;
            if (!(in >> k >> d) || k < 1 || k > NU) err = "usage: act K D";
            else {
                sim.acts.erase(std::remove_if(sim.acts.begin(), sim.acts.end(),
                               [&](const ActAttack& a) { return a.idx == k - 1; }), sim.acts.end());
                sim.acts.push_back({k - 1, sim.t, d});
            }
        } else if (cmd == "actclear") {
            sim.acts.clear();
        } else if (cmd == "replay") {
            std::string tags; double rec_h, jitter = 0.0;
            if (!(in >> tags >> rec_h) || rec_h <= 0) err = "usage: replay TAGS R J";
            else {
                in >> jitter;
                auto parsed = parse_tags(tags);
                if (parsed.empty()) err = "bad tag list";
                else {
                    sim.rp.tags = parsed;
                    sim.rp.rec_h = rec_h;
                    sim.rp.jitter = jitter;
                    sim.rp.t0 = -1.0;
                    sim.rp.active = false;
                    if (!sim.start_replay()) { sim.rp.tags.clear(); err = "not enough recorded history"; }
                }
            }
        } else if (cmd == "replayclear") {
            sim.rp.active = false;
            sim.rp.tags.clear();
        } else {
            err = "unknown command " + cmd;
        }
        out << (err.empty() ? "OK" : "ERR " + err) << "\n" << std::flush;
    }
    return 0;
}

int main(int argc, char* argv[]) {
    double hours = 0.0;
    unsigned long seed = 0;
    std::string out;
    bool live = false;
    Sim sim;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) die("missing value for " + a);
            return argv[++i];
        };
        if (a == "--hours") hours = std::atof(next().c_str());
        else if (a == "--out") out = next();
        else if (a == "--seed") seed = std::strtoul(next().c_str(), nullptr, 10);
        else if (a == "--tstep") sim.tstep = std::atof(next().c_str());
        else if (a == "--log-every") sim.log_every = std::atof(next().c_str());
        else if (a == "--interactive") live = true;
        else if (a == "--idv") {
            std::string v = next();
            int k; double t;
            if (std::sscanf(v.c_str(), "%d@%lf", &k, &t) != 2 || k < 1 || k > 20) die("bad --idv " + v);
            sim.idvs.push_back({k - 1, t});
        } else if (a == "--sp") {
            std::string v = next();
            size_t at = v.find('@'), eq = v.find('=');
            if (at == std::string::npos || eq == std::string::npos) die("bad --sp " + v);
            std::string name = v.substr(0, at);
            if (!valid_sp(name)) die("unknown setpoint " + name);
            sim.sps.push_back({name, std::atof(v.substr(at + 1, eq - at - 1).c_str()),
                               std::atof(v.substr(eq + 1).c_str()), false});
        } else if (a == "--sf") {
            std::string v = next();
            size_t c = v.find(':'), at = v.find('@'), eq = v.find('=');
            if (c == std::string::npos || at == std::string::npos) die("bad --sf " + v);
            int tag = std::atoi(v.substr(0, c).c_str());
            if (tag < 1 || tag > NY) die("bad sensor tag in " + v);
            std::string type = v.substr(c + 1, at - c - 1);
            if (!valid_fault(type)) die("bad sensor fault type " + type);
            double t = std::atof(v.substr(at + 1, (eq == std::string::npos ? v.size() : eq) - at - 1).c_str());
            double p = (eq == std::string::npos) ? 0.0 : std::atof(v.substr(eq + 1).c_str());
            sim.sfs.push_back({tag - 1, type, t, p, false, 0.0});
        } else if (a == "--act") {
            std::string v = next();
            int k; double t, d;
            if (std::sscanf(v.c_str(), "%d@%lf=%lf", &k, &t, &d) != 3 || k < 1 || k > NU) die("bad --act " + v);
            sim.acts.push_back({k - 1, t, d});
        } else if (a == "--replay") {
            std::string v = next();
            size_t at = v.find('@');
            if (at == std::string::npos) die("bad --replay " + v);
            sim.rp.tags = parse_tags(v.substr(0, at));
            if (sim.rp.tags.empty()) die("bad replay tag in " + v);
            if (std::sscanf(v.substr(at + 1).c_str(), "%lf:%lf:%lf", &sim.rp.t0, &sim.rp.rec_h, &sim.rp.jitter) < 2 ||
                sim.rp.rec_h <= 0)
                die("bad --replay " + v);
        } else die("unknown argument " + a);
    }

    if (!(sim.tstep > 0.0 && sim.tstep <= 0.01) || !(sim.log_every >= sim.tscan && sim.log_every <= 24.0))
        die("--tstep must be in (0, 0.01] h and --log-every in [0.0005, 24] h");
    if (live) {
        sim.init(seed, 2.0);
        return interactive(sim);
    }
    if (!(hours > 0.0 && hours <= 10000.0) || out.empty()) die("usage: te_cli --hours H --out FILE.csv [options] | --interactive");
    sim.init(seed, std::max(sim.rp.rec_h, 0.0001));

    std::ofstream f(out);
    if (!f) die("cannot open " + out);
    f.precision(8);
    Sim::header(f);
    long nsteps = (long)std::llround(hours / sim.tstep);
    for (long k = 0; k < nsteps && !sim.shutdown; ++k) sim.tick(f);
    if (sim.shutdown) {
        std::cerr << "te_cli: plant shutdown at t=" << sim.t << " h: " << sim.shutdown_msg << std::endl;
        return 3;
    }
    return 0;
}
