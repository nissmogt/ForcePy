import random
import numpy as np
import numpy.linalg as ln
import json
from math import ceil
from MDAnalysis import Universe
from scipy import weave
from scipy.weave import converters
from math import *
import matplotlib.mlab as mlab
import matplotlib.pyplot as plt


class ForceMatch:
    """Main force match class.
    """
    
    def __init__(self, cguniverse, input_file):
        self.ref_cats = []
        self.tar_cats = []
        self.ref_forces =  []
        self.tar_forces = []
        self.u = cguniverse
        self._load_json(input_file) 
        self.force_match_calls = 0
        self.plot_frequency = 1
        self.plot_output = None
    
    def _load_json(self, input_file):
        with open(input_file, 'r') as f:
            self.json = json.load(f)
        self._test_json(self.json, [])
        self.kt = self.json["kT"]
        if("observable" in self.json):
            self.do_obs = True
            self.obs = [0 for x in range(self.u.trajectory.numframes)]
            with open(self.json["observable"], 'r') as f:
                lines = f.readlines()
                if(len(lines) < len(self.obs)):
                    raise IOError("Number of the frames (%d) does not match number of lines in observation file (%d)" %
                                  (len(self.obs), len(lines)))
                for i, line in zip(range(len(self.obs)), lines[:len(self.obs)]):
                    self.obs[i] = float(line.split()[0])
            if("observable_set" in self.json):
                self.obs = np.apply_along_axis(lambda x:(x - self.json["observable_set"]) ** 2, 0, self.obs)
                print "setting observable to %g" % self.json["observable_set"]

        if("box" in self.json):
            if(len(self.json["box"]) != 3):
                raise IOError("Input file JSON: box must look like \"box\":[5,5,5]. It must have 3 dimensions in an array")

                
                
    def _test_json(self, json, required_keys = [("structure", "Toplogy file"), ("trajectory", "Trajectory File"), ("kT", "Boltzmann's constant times temperature")]):
        for rk in required_keys:
            if(not json.has_key(rk[0])):
                raise IOError("Error in input file, could not find %s\n. Set using %s keyword" % (rk[1], rk[0]))

    def add_tar_force(self, *forces):
        for f in forces:
            self.tar_forces.append(f)
            cat = f.get_category()
            if(not (cat is None)):
                self.tar_cats.append(cat)
            f.setup_hook(self.u)

    def add_ref_force(self, *forces):
        for f in forces:
            self.ref_forces.append(f)
            cat = f.get_category()
            if(not (cat is None)):
                self.ref_cats.append(cat)
            f.setup_hook(self.u)


    def swap_match_parameters_cache(self):
        try:
            for f in self.tar_forces:
                self.cache[f], f.lip = f.lip, self.cache[f]
        except AttributeError:
            self.cache = {}
            for f in self.tar_forces:
                self.cache[f] = np.copy(f.lip)
                        
        
    def force_match(self, iterations = 0):
        
        if(iterations == 0):
            iterations = self.u.trajectory.numframes
        
        ref_forces = np.zeros( (self.u.atoms.numberOfAtoms(), 3) )
        self.u.trajectory.rewind() # just in case this is called after some analysis has been done
        
        #setup plots
        if(self.plot_frequency != -1):

            plot_fig = plt.figure()

            #try to maximize the window
            mng = plt.get_current_fig_manager()
            try:
                mng.frame.Maximize(True)
            except AttributeError:
                try:
                    mng.resize(*mng.window.maxsize())
                except AttributeError:
                    pass


            if(self.plot_output is None):
                plt.ion()                        
            #set-up plots for 16/9 screen
            plot_w = ceil(sqrt(len(self.tar_forces)) * 4 / 3.)
            plot_h = ceil(plot_w * 9. / 16.)
            for i in range(len(self.tar_forces)):
                self.tar_forces[i].plot(plt.subplot(plot_w, plot_h, i+1))
            plt.show()
                

        for ts in self.u.trajectory:
            
            #set box if necessary
            if("box" in self.json):
                #strange ordering due to charm
                self.u.trajectory.ts._unitcell[0] = self.json["box"][0]
                self.u.trajectory.ts._unitcell[2] = self.json["box"][1]
                self.u.trajectory.ts._unitcell[5] = self.json["box"][2]

            self._setup()

            for rf in self.ref_forces:
                rf.calc_forces(ref_forces, self.u)            

            #make plots
                if(self.plot_frequency != -1 and iterations % self.plot_frequency == 0):
                    for f in self.tar_forces:
                        f.update_plot()
                    plt.draw()

            #track error
            net_df = 0
            self.force_match_calls += 1

            #sample particles and run updates on them 
            for i in random.sample(range(self.u.atoms.numberOfAtoms()),self.u.atoms.numberOfAtoms()):
                #calculate net forces deviation
                df = ref_forces[i]
                for f in self.tar_forces:
                    df -= f.calc_particle_force(i,self.u)
                net_df += ln.norm(df)


                #inline C code to accumulate gradient
                code = """
                       for(int i = 0; i < w_length; i++) {
                           grad(i) = 0;
                           for(int j = 0; j < 3; j++)
                               grad(i) -= temp_grad(i,j) * df(j); //negative due to df being switched
                       }
                """

                #now run gradient update step on all the force types
                for f in self.tar_forces:
                    #setup temps for inlince C code
                    w_length = len(f.w)
                    grad = f.w_grad
                    temp_grad = f.temp_grad
                    weave.inline(code, ['w_length', 'grad', 'df', 'temp_grad'],
                         type_converters=converters.blitz,
                         compiler = 'gcc')

                    #the code which is being weaved:
                    #grad = np.apply_along_axis(np.sum, 1, self.temp_grad * df)

                    #apply any regularization
                    for r in f.regularization:
                        grad += r[0](f.w)
                    f.lip +=  np.square(grad)

                    f.w = f.w - f.eta / np.sqrt(f.lip) * grad

            ref_forces.fill(0)
            self._teardown()

            #log of the error
            print "log error at %d  = %g" % (iterations, 0 if net_df < 1 else log(net_df))
            
            iterations -= 1
            if(iterations == 0):
                break
        if(not self.plot_output is None):
            plot_fig.tight_layout()
            plt.savefig(self.plot_output)


    def observation_match(self, obs_sweeps = 25, obs_samples = None, reject_tol = None):
        """ Match observations
        """

        if(obs_samples is None):
            obs_samples = max(5, self.u.trajectory.numframes / obs_sweeps)
        if(reject_tol is None):
            reject_tol = obs_samples
                
        #in case force mathcing is being performed simultaneously,
        #we want to cache any force specific parameters so that we
        #can swap them back in afterwards
        self.swap_match_parameters_cache()

        #we're going to sample the covariance using importance
        #sampling. This requires a normalization coefficient, so
        #we must do multiple random frames

        s_grads = {} #this is to store the sampled gradients. Key is Force and Value is gradient
        for f in self.tar_forces:
            s_grads[f] = [None for x in range(obs_samples)]
        s_obs = [0 for x in range(obs_samples)] #this is to store the sampled observations
        s_weights = [0 for x in range(obs_samples)]
            
        for s in range(obs_sweeps):

            self.force_match_calls += 1
            #make plots
            for f in self.tar_forces:
                try:
                    f.update_plot(true_force=lambda x:4 * (6 * x**(-7) - 12 * x**(-13) ), true_potential=lambda x: 4 * (x**(-12) - x**(-6)))
                except AttributeError:
                     #doesn't have plotting method, oh well
                    pass

            #now we esimtate gradient of the loss function via importance sampling
            normalization = 0
                
            #note, this reading method is so slow. I should implement the frame jump in xyz
            rejects = 0
            i = 0

            while i < obs_samples:

                index = self._sample_ts() #sample a trajectory frame
                self._setup()

                 #get weight
                dev_energy = 0
                for f in self.tar_forces:
                    dev_energy -= f.calc_potential(self.u)
                        
                for f in self.ref_forces:
                    dev_energy += f.calc_potential(self.u)

                    
                if(abs(dev_energy /self.kt) > 250):
                    rejects += 1
                    if(rejects == reject_tol):
                        print "Rejection rate of frames is too high, restarting force matching"
                        self.swap_match_parameters_cache()
                        self.force_match(rejects) #arbitrarily using number of rejects for number matces to use
                        self.swap_match_parameters_cache()
                        rejects = 0
                        continue
                    else:
                        continue

                weight = exp(dev_energy / self.kt)
                s_weights[i] = weight
                normalization += weight

                #store gradient and observabels
                s_obs[i] = weight * self.obs[i]
                for f in self.tar_forces:
                    s_grads[f][i] = weight * np.copy(f.temp_grad[:,1])

                i += 1
                self._teardown()

            #normalize and calculate covariance
            for f in self.tar_forces:
                f.w_grad.fill(0)
                grad = f.w_grad

                #two-pass covariance calculation, utilizing the temp_grad in f
                meanobs = sum(s_obs) / normalization
                meangrad = f.temp_grad[:,2]
                meangrad.fill(0)
                for x in s_grads[f]:
                    meangrad += x  / normalization
                for x,y in zip(s_obs, s_grads[f]):
                    grad += (x - meanobs) * (y - meangrad) / normalization

                #recall we need the negative covariance times the inverse temperature
                grad *= -1. / self.kt

                #now update the weights
                f.lip += np.square(grad)
                change = f.eta / np.sqrt(f.lip) * grad
                f.w = f.w - f.eta / np.sqrt(f.lip) * grad

                print "Obs Mean: %g, reweighted mean: %g" % (sum(self.obs) / len(self.obs) ,meanobs)

            

    def add_and_type_pair(self, force):
        types = []
        for a in self.u.atoms:
            if(not a.type in types):
                types.append(a.type)
        for i in range(len(types)):
            for j in range(i,len(types)):
                if(force.category.pair_exists(self.u, "type %s" % types[i], "type %s" % types[j])):
                    f = force.clone_force()
                    f.specialize_types("type %s" % types[i], "type %s" % types[j])
                    self.add_tar_force(f)

    def _sample_ts(self):        
        self.u.trajectory.rewind()
        index = random.randint(0,self.u.trajectory.numframes - 1)
        [self.u.trajectory.next() for x in range(index)]
        return index
                   

        
            
    def _setup(self):
        for rfcat in self.ref_cats:
            rfcat._setup(self.u)
        for tfcat in self.tar_cats:
            tfcat._setup_update(self.u)        

    def _teardown(self):
        for rfcat in self.ref_cats:
            rfcat._teardown()
        for tfcat in self.tar_cats:
            tfcat._teardown_update()        


#abstract classes

class ForceCategory(object):
    """A category of force/potential type.
    
    The forces used in force matching are broken into categories, where
    the sum of each category of forces is what's matched in the force
    matching code. Examples of categories are pairwise forces,
   threebody forces, topology forces (bonds, angles, etc).
   """
    pass



class NeighborList:
    """Neighbor list class
    """
    def __init__(self, u, cutoff, exclude_14 = True):
        
        #set up cell number and data
        self.cutoff = cutoff
        self.box = [(0,x) for x in u.dimensions[:3]]
        self.img = u.dimensions[:3]
        self.nlist_lengths = np.arange(0, dtype='int32')
        self.nlist = np.arange(0, dtype='int32')
        self.cell_number = [max(1,int(ceil((x[1] - x[0]) / self.cutoff))) for x in self.box]        
        self.bins_ready = False
        self.cells = np.empty(u.atoms.numberOfAtoms(), dtype='int32')
        self.head = np.empty( reduce(lambda x,y: x * y, self.cell_number), dtype='int32')
        self.exclusion_list = None
        self.exclude_14 = exclude_14

        #pre-compute neighbors. Waste of space, but saves programming effort required for ghost cellls
        self.cell_neighbors = [[] for x in range(len(self.head))]
        for xi in range(self.cell_number[0]):
            for yi in range(self.cell_number[1]):
                for zi in range(self.cell_number[2]):
                    #get neighbors
                    index = (xi * self.cell_number[0] + yi) * self.cell_number[1] + zi
                    index_vector = [xi, yi, zi]
                    neighs = [[] for x in range(3)]
                    for i in range(3):
                        neighs[i] = [self.cell_number[i] - 1 if index_vector[i] == 0 else index_vector[i] - 1,
                                     index_vector[i],
                                     0 if index_vector[i] == self.cell_number[i] - 1 else index_vector[i] + 1]
                    for xd in neighs[0]:
                        for yd in neighs[1]:
                            for zd in neighs[2]:
                                neighbor = xd * self.cell_number[0] ** 2 + \
                                                                       yd * self.cell_number[1]  + \
                                                                       zd
                                if(not neighbor in self.cell_neighbors[index]): #this is possible if wrapped and cell number is 1
                                    self.cell_neighbors[index].append(neighbor)



    def dump_cells(self):
        print self.cell_number
        print self.box
        print self.cell_neighbors

    def bin_particles(self, u):
        self.head.fill(-1)
        positions = u.atoms.get_positions(copy=False)
        positions = u.atoms.coordinates()
        for i in range(u.atoms.numberOfAtoms()):
            icell = 0
            #fancy index and binning loop over dimensions
            for j in range(3):                
                #sometimes things are unwrapped, better to assume they aren't
                k = (positions[i][j] -  self.box[j][0]) / (self.box[j][1] - self.box[j][0]) * self.cell_number[j]
                k = floor(k % self.cell_number[j])      
                icell =  int(k) + icell * self.cell_number[j]
            #push what is on the head into the cells
            self.cells[i] = self.head[icell]
            #add current value
            self.head[icell] = i

        self.bins_ready = True

    def _build_exclusion_list(self, u):
        #what we're building
        self.exclusion_list = [[] for x in range(u.atoms.numberOfAtoms())]
        #The exclusion list at the most recent depth
        temp_list = [[] for x in range(u.atoms.numberOfAtoms())]
        #build 1,2 terms
        for b in u.bonds:            
            self.exclusion_list[b.atom1.number].append(b.atom2.number)
            self.exclusion_list[b.atom2.number].append(b.atom1.number)
        # build 1,3 and 1,4
        for i in range( 1 if self.exclude_14 else 2):
            #copy
            temp_list[:] = self.exclusion_list[:]
            for a in range(u.atoms.numberOfAtoms()):
                for b in range(len(temp_list[a])):
                    self.exclusion_list[a].append(b) 

    def build_nlist(self, u):
        
        if(self.exclusion_list == None):
            self._build_exclusion_list(u)

        self.nlist = np.resize(self.nlist, (u.atoms.numberOfAtoms() - 1) * u.atoms.numberOfAtoms())
        #check to see if nlist_lengths exists yet
        if(len(self.nlist_lengths) != u.atoms.numberOfAtoms() ):
            self.nlist_lengths.resize(u.atoms.numberOfAtoms())
        
        if(not self.bins_ready):
            self.bin_particles(u)

        self.nlist_lengths.fill(0)
        positions = u.atoms.get_positions(copy=False)
        nlist_count = 0
        for i in range(u.atoms.numberOfAtoms()):
            icell = 0
            #fancy indepx and binning loop over dimensions
            for j in range(3):
                #sometimes things are unwrapped, better to assume they aren't
                k = (positions[i][j] -  self.box[j][0]) / (self.box[j][1] - self.box[j][0]) * self.cell_number[j]
                k = floor(k % self.cell_number[j])      
                icell =  int(k) + icell * self.cell_number[j]
            for ncell in self.cell_neighbors[icell]:
                j = self.head[ncell]
                while(j != - 1):
                    if(i != j and 
                       not (j in self.exclusion_list[i]) and
                       min_img_dist_sq(positions[i],positions[j],self.img) < self.cutoff ** 2):
                        self.nlist[nlist_count] = j
                        self.nlist_lengths[i] += 1
                        nlist_count += 1
                    j = self.cells[j]

        self.nlist = self.nlist[:nlist_count]
        return self.nlist, self.nlist_lengths


class Pairwise(ForceCategory):
    """Pairwise force category. It handles constructing a neighbor-list at each time-step. 
    """
    instance = None

    @staticmethod
    def get_instance(*args):        
        if(Pairwise.instance is None):
            Pairwise.instance = Pairwise(args[0])
        else:
            #check cutoff
            if(Pairwise.instance.cutoff != args[0]):
                raise RuntimeError("Incompatible cutoffs")
        return Pairwise.instance
    
    def __init__(self, cutoff=12):
        super(Pairwise, self).__init__()
        self.cutoff = cutoff                    
        self.forces = []
        self.nlist_ready = False
        self.nlist_obj = None

    def _build_nlist(self, u):
        if(self.nlist_obj is None):
            self.nlist_obj = NeighborList(u, self.cutoff)
        self.nlist, self.nlist_lengths = self.nlist_obj.build_nlist(u)

        self.nlist_ready = True                    

    def _setup(self, u):
        if(not self.nlist_ready):
            self._build_nlist(u)

    def _teardown(self):
        self.nlist_ready = False
        
    def _setup_update(self,u):
        self._setup(u)


    def _teardown_update(self):
        self._teardown()

    def pair_exists(self, u, type1, type2):
        return True

    
class Bond(ForceCategory):

    """Bond category. It caches each atoms bonded neighbors when constructued
    """
    instance = None

    @staticmethod
    def get_instance(*args):        
        if(Bond.instance is None):
            Bond.instance = Bond()
        return Bond.instance
    
    def __init__(self):
        super(Bond, self).__init__()
        self.blist_ready = False
    

    def _build_blist(self, u):
        temp = [[] for x in range(u.atoms.numberOfAtoms())]
        #could be at most everything bonded with everything
        self.blist = np.empty((u.atoms.numberOfAtoms() - 1) * (u.atoms.numberOfAtoms() / 2), dtype=np.int32)
        self.blist_lengths = np.empty(u.atoms.numberOfAtoms(), dtype=np.int32)
        blist_accum = 0
        for b in u.bonds:
            temp[b.atom1.number].append(b.atom2.number)
            temp[b.atom2.number].append(b.atom1.number)

        #unwrap the bond list to make it look like neighbor lists
        for i,bl in zip(range(u.atoms.numberOfAtoms()), temp):
            self.blist_lengths[i] = len(temp[i])
            for b in bl:
                self.blist[blist_accum] = b
                blist_accum += 1

        #resize now we know how many bond items there are
        self.blist = self.blist[:blist_accum]
        self.blist_ready = True

    def _setup(self, u):
        if(not self.blist_ready):
            self._build_blist(u)

    def _teardown(self):
        self.nlist_ready = False
        
    def _setup_update(self,u):
        self._setup(u)

    def _teardown_update(self):
        self._teardown()

    @property
    def nlist(self):
        return self.blist

    @property
    def nlist_lengths(self):
        return self.blist_lengths

    def pair_exists(self, u, type1, type2):
        """Check to see if a there exist any pairs of the two types given
        """
        if(not self.blist_ready):
            self._build_blist(u)        

        sel2 = u.atoms.selectAtoms(type2)        
        for a in u.atoms.selectAtoms(type1):
            i = a.number
            blist_accum = np.sum(self.blist_lengths[:i]) if i > 0  else 0
            for j in self.blist[blist_accum:(blist_accum + self.blist_lengths[i])]:
                if(u.atoms[int(j)] in sel2):
                    return True

        return False



def min_img_vec(x, y, img, periodic=True):
    dx = x - y
    if(periodic):
        dx -= np.round(dx / img[:3]) * img[:3]
    return dx
    
def min_img_dist(x, y, img, periodic=True):
    if(not periodic):
        return sqrt(np.sum((x - y)**2))

    code = """
           #line 553 "ForceMatch.py"
           double sum = 0;
           double dx;
           double div;          
           for(int i = 0; i < 3; i++) {
               dx = x(i) - y(i);
               div = dx / img(i);
               dx -= div < 0.0 ? ceil(div - 0.5) : floor(div + 0.5);
               sum += dx;
            }
            return_val = sqrt(sum);
            """
    return weave.inline(code, ['x', 'y', 'img'],
                        type_converters=converters.blitz,
                        compiler = 'gcc',
                        headers=['<math.h>'],
                        libraries=['m'])

    #return ln.norm(min_img_vec(x,y,img, periodic))

def min_img_dist_sq(x, y, img, periodic=True):

    if(not periodic):
        return np.sum((x - y)**2)

    code = """
           #line 579  "ForceMatch.py"
           double sum = 0;
           double dx;
           double div;          
           for(int i = 0; i < 3; i++) {
               dx = x(i) - y(i);
               div = dx / img(i);
               dx -= div < 0.0 ? ceil(div - 0.5) : floor(div + 0.5);
               sum += dx;
            }
            return_val = sum;
            """
    return weave.inline(code, ['x', 'y', 'img'],
                        type_converters=converters.blitz,
                        compiler = 'gcc',
                        headers=['<math.h>'],
                        libraries=['m'])

    #return np.sum(min_img_vec(x,y,img, periodic)**2)

def min_img(x, img, periodic=True):
    if(periodic):
        x -= np.floor(dx / img[:3]) * img[:3]
    return x
