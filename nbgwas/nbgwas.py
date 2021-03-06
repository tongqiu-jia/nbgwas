"""Network boosted GWAS package

This package takes GWAS SNP level summary statistics and re-prioritizes 
using network data

Notes
-----
    This entire package is a WIP (and untested).

"""

from __future__ import print_function
from collections import defaultdict
import ndex2
import ndex2.client as nc
import networkx as nx
import numpy as np
import pandas as pd 
from scipy.sparse import coo_matrix,csc_matrix
from scipy.sparse.linalg import expm, expm_multiply
import time
import warnings

def _read_link(link): 
    """Checks link to be string or pandas DataFrame"""

    if isinstance(link, str): 
        return Nbgwas._read_table(link) 
    elif isinstance(link, pd.DataFrame): 
        return link 
    elif link is None: 
        return link
    else: 
        raise TypeError("Only strings (presumed to be file location) " + \
                            "or pandas DataFrame is allowed!")

def _read_snp_file(file): 
    return pd.read_csv(file, header=0, index_col=None, sep='\s+')

def _read_pc_file(file): 
    return pd.read_csv(file, index_col=0, sep='\s+', 
                        names=['Chromosome', 'Start', 'End'])

def assign_snps_to_genes(snp, 
                         pc, 
                         window_size=0, 
                         to_table=False, 
                         agg_method='min', 
                         chrom_col='hg18chr', 
                         bp_col='bp', 
                         pval_col='pval'): 

    """Assigns SNP to genes

    Parameters
    ----------
    snp : pd.DataFrame
        pandas DataFrame of SNP summary statistics from GWAS. It must have the 
        following three columns
        - chromosome (chrom_col): the chromosome the SNP is on
        - basepair (bp_col): the base pair number 
        - p-value (pval_col): the GWAS associated p-value
    pc : pd.DataFrame
        pandas DataFrame of gene coding region
    window_size : int or float
        Move the start site of a gene back and move the end site forward
        by a fixed `window_size` amount. 
    agg_method : str or callable function
        Method to aggregate multiple p-values associated with a SNP
        - min : takes the minimum p-value
        - median : takes the median of all associated p-values
        - mean : takes the average of all assocaited p-values
        - <'callable' function> : a function that takes a list and output 
          a value. The output of this value will be used in the final dictionary.
    to_table : bool
        If to_table is true, the output is a pandas dataframe that augments the pc 
        dataframe with number of SNPs, top SNP P-value, and the position of the SNP 
        for each gene. Otherwise, a dictionary of gene to top SNP P-value is returned.
          
    Output
    ------
    assigned_pvals : dict or pd.Dataframe
        A dictionary of genes to p-value (see to_table above)

    TODO
    ----
    - Test me!
    - Expose column names for pc
    - Change pc to something more descriptive
    - Add an option for caching bin edges
    """
    
    if agg_method not in ['min', 'median', 'mean'] and not hasattr(agg_method, '__call__'): 
        raise ValueError('agg_method must be min, median, mean or a callable function!')
    
    window_size = int(window_size)
    
    assigned_pvals = defaultdict(lambda: [[], []])
    for chrom, df in snp.groupby(chrom_col): 
        bins, names = _get_bins(pc.loc[pc.iloc[:,0] == str(chrom)], window_size=window_size) #TODO: HARDCODE
        bps = df[bp_col].values
        binned = np.digitize(bps, bins)
        
        names = names[binned]
        pvals = df[pval_col].values
        
        index = np.array([ind for ind, i in enumerate(names) if i != []])
        
        for i in index: 
            for n in names[i]: 
                assigned_pvals[n][0].append(pvals[i])
                assigned_pvals[n][1].append(bps[i])
                
    # Aggregate p-values
    if agg_method == 'min': 
        f = np.argmin
    elif agg_method == 'median': 
        f = lambda x: np.argwhere(np.median(x)).ravel()
    elif agg_method == 'mean': 
        f = lambda x: np.argwhere(np.mean(x)).ravel()
    else: 
        f = agg_method    

    for i,j in assigned_pvals.items(): 
        if not to_table: 
            assigned_pvals[i] = j[0][f(j[0])]
        else: 
            p = f(j[0])
            assigned_pvals[i] = [len(j[0]), j[0][p], j[1][p]] #nSNPS, TopSNP-pvalue, TopSNP-pos

    if to_table: 
        assigned_df = pd.DataFrame(assigned_pvals, index=['nSNPS', 'TopSNP P-Value', 'TopSNP Position']).T
        assigned_df = pd.concat([pc, assigned_df], axis=1)
        assigned_df.index.name = 'Gene'
        assigned_df = assigned_df.reset_index()
    
        return assigned_df
    
    return assigned_pvals

def _get_bins(df, window_size=0, cols=[1,2]): 
    """Convert start and end sites to bin edges 
    
    Given the start and end site (defined by cols) in the dataframe, 
    a set of bin edges are defined which can be augmented by window_size. 
    Each bin is then annotated by a name (assumed to be in the index. 
    Note that each bin can have multiple names due to overlapping start 
    and end sites. If the name is empty, then that bin is not occupied by 
    a gene.
    """
    names = df.index.values
    
    arr = df.iloc[:, cols].values.astype(int)
    
    arr[:, 0] -= window_size
    arr[:, 1] += window_size
    
    bins = np.sort(arr.reshape(-1))
    
    mapped_names = [[] for _ in range(len(bins) + 1)] 
    
    for ind, (i,j) in enumerate(arr): 
        vals = np.argwhere((bins > i) & (bins <= j)).ravel()
        
        for v in vals: 
            mapped_names[v].append(names[ind])
            
    return bins, np.array(mapped_names)

# Load gene positions from file                                                                                            
def load_gene_pos(gene_pos_file, delimiter='\t', header=False, cols='0,1,2,3'):
    """Loads the gene position dataframe"""

    # Check for valid 'cols' parameter                                                                                 
    try:
        cols_idx = [int(c) for c in cols.split(',')]
    except:
        raise ValueError('Invalid column index string')
    # Load gene_pos_file                                                                                               
    if header:
        gene_positions = pd.read_csv(gene_pos_file, delimiter=delimiter)
    else:
        gene_positions = pd.read_csv(gene_pos_file, delimiter=delimiter, header=-1)
    # Check gene positions table format                                                                                
    if (gene_positions.shape[1] < 4) | (max(cols_idx) >  gene_positions.shape[1]-1):
        raise ValueError('Not enough columns in Gene Positions File')

    # Construct gene position table                                                                                    
    gene_positions = gene_positions[cols_idx]
    gene_positions.columns = ['Gene', 'Chr', 'Start', 'End']
    
    return gene_positions.set_index('Gene')

class Nbgwas(object): 
    """Interface to Network Boosted GWAS

    Parameters
    ----------
    snp_level_summary : str or pd.DataFrame
        A DataFrame object that holds the snp level summary or a file that points 
        to a text file
    gene_level_summary : str or pd.DataFrame
        A DataFrame object that holds the gene level summary or a file that points 
        to a text file     
    network : networkx object
        The network to propagate the p-value over. If None, PC-net (Huang, Cell Systems, 2018) 
        will be pulled from NDEx instead.

    Note
    ----
    Please be aware the interface is very unstable and will be changed. 

    TODO
    ----
    - Refactor out p-value assignment (with different methods) as different functions
    - Combines the heat diffusion code as one function (with a switch in behavior for kernel vs  no kernel)
    - Missing output code (to networkx subgraph, Upload to NDEx)
    - Missing utility functions (Manhanttan plots)   
    - Include logging
    """
    def __init__(self, 
                 network = 'PC_net',
                 snp_level_summary=None, 
                 gene_level_summary=None, 
                 protein_coding_table=None, 
                 window_size=0,  
                 agg_method='min', 
                 chrom_col='hg18chr', 
                 bp_col='bp', 
                 pval_col='pval', 
                 verbose=True
                 ): 
        
        if snp_level_summary is not None and gene_level_summary is not None: 
            warnings.warn("snp_level_summary argument will be ignored since both \
snp_level_summary and gene_level_summary are provided!")
        elif snp_level_summary is None and gene_level_summary is None: 
            raise ValueError("Either snp_level_summary or gene_level_summary must be provided!")

        self.snp_level_summary = _read_snp_file(snp_level_summary)
        self.gene_level_summary = _read_link(gene_level_summary) 

        if network is None: 
            raise ValueError("A network must be given!")
        elif network == 'PC_net': 
            self.network = self._load_pcnet()
        elif isinstance(network, nx.Graph): 
            self.network = network
        else: 
            raise TypeError("Network must be a networkx Graph object")

        self.node_names = [self.network.node[n]['name'] for n in self.network.nodes()]
        self.protein_coding_table = _read_pc_file(protein_coding_table)

        self.window_size = window_size
        self.agg_method = agg_method
        self.chrom_col = chrom_col
        self.bp_col = bp_col
        self.pval_col = pval_col
        self.verbose = verbose

    @staticmethod
    def _read_table(file):
        min_p_table = pd.read_csv(file, 
                                  sep='\t',
                                  usecols=[1,2,3,4,5,6,7,8,9])

        return min_p_table

    @staticmethod
    def _load_pcnet(): 
        anon_ndex = nc.Ndex2("http://public.ndexbio.org")
        network_niceCx = ndex2.create_nice_cx_from_server(server='public.ndexbio.org', 
                                                          uuid='f93f402c-86d4-11e7-a10d-0ac135e8bacf')

        return network_niceCx.to_networkx()

    def assign_pvalues(self, **kwargs): 
        """Wrapper for assign_snps_to_genes"""

        if self.protein_coding_table is None: 
            raise ValueError("protein_coding_table attribute is missing!")
        
        assign_pvalues = assign_snps_to_genes(self.snp_level_summary, 
                                              self.protein_coding_table,
                                              to_table=True,
                                              **kwargs)

        if self.gene_level_summary is not None: 
            warnings.warn("The existing gene_level_summary was overwritten!")

        self.gene_level_summary = assign_pvalues

        return self


    def diffuse(self, threshold=5e-6, kernel=None): 
        """Runs random walk with pre-computed kernel 
        
        This propagation method relies on a pre-computed kernel. 

        Parameters
        ----------
        threshold : float
            Minimum p-value threshold to diffuse the p-value
        kernel : str
            Location of the kernel (expects to be in HDF5 format)        
        """
        if kernel is not None: 
            self.kernel = pd.read_hdf(kernel)
            network_genes = list(self.kernel.index)

        else: 
            raise NotImplementedError("Need to define the location to the kernel! TODO: Add non-pre-computed kernel code")


        if not hasattr(self, "laplacian"): 
            self.laplacian = csc_matrix(nx.laplacian_matrix(self.network))

        name='prop'
        threshold_genes = {}
        prop_vectors = []
            
        threshold_genes[name] = self.gene_level_summary[self.gene_level_summary['TopSNP P-Value'] < threshold]
        prop_vector = (self.gene_level_summary.set_index('Gene').loc[network_genes, 'TopSNP P-Value'] < threshold).astype(float)
        prop_vector.name = name
        prop_vectors.append(prop_vector)
        prop_vector_matrix = pd.concat(prop_vectors, axis=1).loc[network_genes].T

        #propagate with pre-computed kernel
        prop_val_matrix = np.dot(prop_vector_matrix, self.kernel)
        prop_val_table = pd.DataFrame(prop_val_matrix, index = prop_vector_matrix.index, columns = prop_vector_matrix.columns)
        
        self.boosted_pvalues = prop_val_table.T.sort_values(by='prop', ascending=False)

        return self

    def heat_diffusion(self, threshold=5e-6): 
        """Runs heat diffusion without a pre-computed kernel
        
        Parameters
        ----------
        threshold : float
            Minimum p-value to diffuse the p-value
        """
        if not hasattr(self, "laplacian"): 
            self.laplacian = csc_matrix(nx.laplacian_matrix(self.network))
            
        input_list = list(self.gene_level_summary[self.gene_level_summary['TopSNP P-Value'] < threshold]['Gene'])
        input_vector = np.array([n in input_list for n in self.node_names])
        out_vector=expm_multiply(-self.laplacian, input_vector, start=0, stop=0.1, endpoint=True)[-1]

        #out_dict= dict(zip(node_names, out_vector))
        out_dict= {'prop': out_vector,'Gene':self.node_names}
        heat_df=pd.DataFrame.from_dict(out_dict).set_index('Gene')

        self.boosted_pvalues = heat_df.sort_values(by='prop', ascending=False).head()

        return self
