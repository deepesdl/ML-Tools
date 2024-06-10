import os
import sys
import time
import scipy
import random
import shutil
import numpy as np
import xarray as xr
import pandas as pd
import multiprocessing as mp
from tqdm import tqdm
from sklearn.svm import SVR
from scipy import interpolate
from sklearn import preprocessing
from multiprocessing.dummy import Pool
from sklearn.model_selection import KFold, cross_val_score
from sklearn.model_selection import GridSearchCV, RandomizedSearchCV



"""
In this file the gaps created in the GapDataset class will be filled.
The Gapfiller class builds for each missing value an own model.
So far only Support Vector Regression is tested but other ML algorithms, hyperparameters and predictors can be added.
"""


class Gapfiller:
    """
    The Gapfiller class fills gaps in cube data using machine learning models.
    It provides methods for data preprocessing, hyperparameter tuning, cross-validation, and prediction.

    Parameters:
        ds_name (str): The name of the dataset.
        learning_function (str): The type of learning function.
        hyperparameters (str): Hyperparameter search method ('RandomGridSearch' | 'FullGridSearch' | 'Custom').
        predictor (str): Predictor strategy ('AllPoints' | 'LCC' | 'RandomPoints').

    Attributes:
        actual_matrix (np.ndarray): The actual data matrix with gaps.
        data_with_gaps (dict): A dictionary containing data matrices with different gap sizes.
        directory (str): The directory where results and data are stored.
        gap_value (float): The gap value in the data (np.nan for real gaps and -100 for artificial gaps).
        metadata (dict): Metadata associated with the dataset.
        pool (multiprocessing.dummy.Pool): A pool of worker processes for parallelization.
        scores (dict): Dictionary to store MAE scores for gap filling results.
        temp_array_with_gaps (np.ndarray): Temporary data array with gaps for gap filling.
        temp_known_pixels (int): Number of known pixels in the temporary data array.
        training_data (ndarray): Historical data matrices.

    Methods:
    - gapfill(): Perform the gap filling process using the learning function for different gap sizes.
    - get_arrays(): Retrieve data arrays and initialize necessary variables.
    - print_insights(): Print insights about the gap-filling process.
    - process_gap_array(gap_size): Process the gap-filling process for different gap sizes.
    - fill_the_gaps(gap_indices): Fill the gaps in the data array using a pixel model.
    - pixel_model(gap_index): Predict missing values for a specific pixel based on the chosen predictor.
    - create_dataframe(coords): Create a dataframe from selected coordinates.
    - get_random_points(): Get random non-gap coordinates for use as predictors.
    - get_extra_matrix_points(gap_index): Get coordinates based on Land Cover Classification (LCC) for modeling.
    - preprocess_dataframe(dataframe): Preprocess the dataframe, handling NaN values and cloud-covered areas.
    - get_train_test_sets(dataframe): Split the dataframe into training and testing sets.
    - process_results(gap_size, filled_array, actual_scores, validation_scores, start_time): Process and print gap filling results.
    - train_model(X_train, y_train, X_test): Perform SVR training, hyperparameter tuning, and prediction.
    - interpolation(gap_index): Perform nearest neighbor interpolation for gap filling.

    Example:
    ```
    Gapfill(ds_name='Test123', learning_function='SVR' hyperparameters='RandomGridSearch', predictor='LCC').gapfill()
    ```
    """
    def __init__(self, ds_name="Test123", learning_function="SVR", hyperparameters="RandomGridSearch", predictor="RandomPoints"):
        self.ds_name = ds_name
        self.learning_function = learning_function
        self.hyperparameters = hyperparameters
        self.predictor = predictor

        self.actual_matrix = None
        self.data_with_gaps = {}
        self.directory = os.path.dirname(os.getcwd()) + '/application_results/' + ds_name + "/" if \
            os.getcwd().split('/')[-1] != 'gapfilling' else 'application_results/' + ds_name + "/"
        self.gap_value = np.nan
        self.metadata = {}
        self.pool = None
        self.scores = {}
        self.temp_gap_array = None
        self.temp_known_pixels = None
        self.training_data = []

    def gapfill(self):
        """
        Fill gaps in the data using machine learning models.
        The method fills each gap by building a model individually.
        """
        # Retrieve and organize the data arrays
        self.get_arrays()
        # Create a directory or clean it if it already exists
        shutil.rmtree(self.directory + "Results/", ignore_errors=True)
        os.makedirs(self.directory + "Results/", exist_ok=True)
        self.print_insights()

        # Loop through different gap sizes
        for gap_size in self.data_with_gaps:
            start_time = time.time()
            gap_indices = self.process_gap_array(gap_size)
            # Create pool of worker for parallelization and use parallel processing to fill gaps for each pixel
            self.pool = Pool(mp.cpu_count())
            filled_array, actual_scores, validation_scores = self.fill_the_gaps(gap_indices)
            # Process and print results for the current gap size
            self.process_results(gap_size, filled_array, actual_scores, validation_scores, start_time)
            # Close the pool of worker processes
            self.pool.close()
        print(f"The missing values of the gaps are now filled. You can find the results in /{self.directory}Results/")

    def get_arrays(self):
        """
        Retrieve and organize data arrays needed for gap filling.

        This method loads historical data as training data, the actual data matrix and if artificial gaps were created,
        data with gaps for different gap sizes. It prepares these arrays for the gap filling process.

        Returns:
            None
        """
        # Open the cube and identify the variables
        self.actual_matrix = xr.open_dataset(self.directory + "actual.nc")
        actual_date = np.datetime_as_string(self.actual_matrix.time, unit='D')
        gap_imitation_directory = self.directory + "GapImitation/"
        variable = [v for v in list(self.actual_matrix.variables) if v not in ["lat", "lon", "time"]][0]

        # Load and process gap imitation arrays if they exist
        if os.path.exists(gap_imitation_directory):
            # Set gap value to distinguish from nans
            self.gap_value = -100
            files = sorted(os.listdir(gap_imitation_directory))
            # Process the arrays and gap sizes
            for file in files:
                gap_size = file[:-3].split("_")[-1]
                self.data_with_gaps[gap_size] = xr.open_dataset(gap_imitation_directory + file)[variable].to_numpy()
        else:
            # Calculate the absolute and relative number of gaps (NaNs) in the variable and process it
            gaps_absolute = np.isnan(self.actual_matrix[variable]).sum().item()
            gap_size = round(gaps_absolute / self.actual_matrix[variable].size, 3)
            self.data_with_gaps[gap_size] = self.actual_matrix[variable].to_numpy()

        # Open the data cube and extract data for each time step in the cube and save it in a NumPy array
        cube = xr.open_dataset(self.directory + "cube.nc")
        for t in cube.time:
            # select the historical data as trainings data but avoid including the original array
            if actual_date != np.datetime_as_string(t, unit='D'):
                array = cube.sel(time=t.data)[variable].to_numpy()
                self.training_data.append(array)
        self.training_data = np.array(self.training_data)

    def print_insights(self):
        """
        Print insights about the gap-filling process.

        This method formats and prints the details about the gaps that were filled, including the number of arrays with
        gaps, the size of the gaps, the directory where the filled arrays are saved, and the date of the actual matrix.

        Args:
            None

        Returns:
            None
        """
        formatted_gaps = [str(round(float(g) * 100, 1)) + "%" for g in list(self.data_with_gaps.keys())]
        actual_date = np.datetime_as_string(self.actual_matrix.time, unit='D')
        print(f"Fill the gaps of {len(formatted_gaps)} array(s) with the following gap size: {', '.join(formatted_gaps)}")
        print(f"The array(s) are saved in: /{self.directory}")
        print(f"Date: {actual_date} \n")

    def process_gap_array(self, gap_size):
        """
        Process the gap array and return indices of gap pixels.

        Args:
            gap_size (float): percentage of the gap size

        Returns:
            gap_indices (ndarray): indices of gap pixels
        """
        # Calculate the number of gap pixels
        gap_size_pixel = int(float(gap_size) * self.training_data[0].size)
        print(f"gap size: {round(float(gap_size) * 100, 1)} % "
              f"-> {gap_size_pixel} pixel \ntraining pictures: {self.training_data.shape[0]}")
        # Set the current data array with gaps for gap filling as a class variable
        self.temp_gap_array = self.data_with_gaps[gap_size]

        # Calculate number of known pixels (non-artificial-gap and non-NaN-value)
        if np.isnan(self.gap_value):
            self.temp_known_pixels = self.temp_gap_array.size - np.isnan(self.temp_gap_array).sum()
        else:
            # If gap_value is not NaN, count all pixels that are neither gap_value nor NaN
            self.temp_known_pixels = (self.temp_gap_array.size - (self.temp_gap_array == self.gap_value).sum()
                                      - np.isnan(self.temp_gap_array).sum())

        # Find indices of pixels with gaps (actual gaps or artificial gaps)
        if np.isnan(self.gap_value):
            gap_indices = np.argwhere(np.isnan(self.temp_gap_array))
        else:
            gap_indices = np.argwhere(self.temp_gap_array == self.gap_value)
        return gap_indices

    def fill_the_gaps(self, gap_indices):
        """
        Fill the gaps in the data array using a pixel model.

        This method uses parallel processing to fill gaps for each pixel specified in the gap_indices.
        It utilizes a pool of workers to predict values for each gap pixel and updates the data array
        accordingly. It also tracks actual and validation scores.

        Args:
            gap_indices (ndarray): List of indices (tuples) specifying the positions of the gaps in the array.

        Returns:
                - filled_array (ndarray): The data array with gaps filled.
                - actual_scores (list): List of actual scores for each filled pixel.
                - validation_scores (list): List of validation scores for each filled pixel.
        """
        # Lists to store actual and validation scores for each pixel and a copy of the data array to fill gaps
        actual_scores = []
        validation_scores = []
        filled_array = np.copy(self.temp_gap_array)

        # Use parallel processing to fill gaps for each pixel in the gap_indices
        with self.pool as pool:
            results = pool.imap(self.pixel_model, gap_indices)
            # Process the progress within a status bar
            with tqdm(total=len(gap_indices), file=sys.stdout, colour='GREEN',
                      bar_format='{l_bar}{bar:20}{r_bar}') as pbar:
                for gap_pred_score in results:
                    gap_index = gap_pred_score[0]
                    # Update the filled_array with the predicted values for the current pixel
                    filled_array[gap_index[0], gap_index[1]] = gap_pred_score[1]
                    # Append actual and cross validation scores to their respective lists
                    actual_scores.append(gap_pred_score[2])
                    validation_scores.append(gap_pred_score[3])
                    pbar.update(1)

        return filled_array, actual_scores, validation_scores

    def pixel_model(self, gap_index):
        """
        Fill a single gap in the data using machine learning models.

        This method fills a single gap specified by the `gap_index` by training a machine learning model.
        It handles different predictor strategies and gap interpolation.

        Parameters:
        - gap_index (tuple): The index (row, column) of the gap to be filled.

        Returns:
        tuple: A tuple containing the following elements:
            1. gap_index (tuple): The index (row, column) of the filled gap.
            2. prediction (float): The predicted value for the filled gap.
            3. actual_score (float or str): The absolute error between the actual value (if available) and the prediction.
            4. validation_score (float or str): The cross-validation score (if available).
        """
        # Determine the predictor coordinates based on the selected strategy
        if self.predictor == "AllPoints":
            # Find all non-gap points
            if np.isnan(self.gap_value):
                coords = np.argwhere(~np.isnan(self.temp_gap_array))
            else:
                coords = np.argwhere(self.temp_gap_array != self.gap_value)
        elif self.predictor == "RandomPoints":
            coords = self.get_random_points()  # Get random non-gap points
        elif self.predictor == "LCC":
            coords = self.get_extra_matrix_points(gap_index)  # Get points based on LCC

        # If no valid coordinates are available, perform gap interpolation
        if type(coords) == str:
            prediction = self.interpolation(gap_index)
            validation_score = "not available"
            if self.gap_value != -100:
                actual_value = self.actual_matrix[gap_index[0], gap_index[1]]
                actual_score = abs(actual_value - prediction)
            else:
                actual_score = "not available"
            return gap_index, prediction, actual_score, validation_score

        if self.predictor != "LCC":
            coords = list(coords)
            coords.append([gap_index[0], gap_index[1]])

        # Create a dataframe using the selected coordinates
        dataframe = self.create_dataframe(coords)
        # Rename columns to use numeric indices
        new_columns = range(0, len(dataframe.columns))
        col_names = [str(i) for i in new_columns]
        dataframe.set_axis(col_names, axis=1, copy=False)
        # Replace -100 values with NaN
        dataframe.replace(-100, np.nan, inplace=True)
        # Remove rows where all values are NaN (indicating cloud-covered areas)
        dataframe = dataframe.dropna(how='all')
        # Preprocess the dataframe
        dataframe = self.preprocess_dataframe(dataframe)

        # Split the dataframe into training and testing sets
        X_train, y_train, X_test = self.get_train_test_sets(dataframe)

        # If there are no valid training samples, perform gap interpolation
        if not X_train.any():
            prediction = self.interpolation(gap_index)
            validation_score = "not available"
        else:
            # Apply the learning function to predict the gap value and calculate the validation score
            prediction, validation_score = self.train_model(X_train, y_train, X_test)

        # Interpolate where there is no prediction
        if prediction is None:
            prediction = self.interpolation(gap_index)
            validation_score = "not available"
        else:
            prediction = prediction.item()

        if self.gap_value == -100:
            variable = [variable for variable in list(self.actual_matrix.variables) if variable not in ["lat", "lon", "time"]][0]
            actual_value = self.actual_matrix[variable].data[gap_index[0], gap_index[1]]
            actual_score = abs(actual_value - prediction)
        else:
            actual_score = "not available"
        return gap_index, prediction, actual_score, validation_score

    def create_dataframe(self, coords):
        """
        Create a pandas DataFrame for machine learning model training.

        This method creates a DataFrame using the specified coordinates for training a machine learning model.
        It prepares the data for model training and prediction.

        Parameters:
        - coords (list of lists): List of coordinates [row, column] used as predictors.

        Returns:
        pd.DataFrame: A DataFrame containing historical data and target values for training.
        """
        # Convert the input coordinates to a numpy array for easy indexing
        coords = np.array(coords)
        # Create a dataframe with dimensions (number of historical data points + 1) x (number of coordinates)
        dataframe = np.full((len(self.training_data) + 1, len(coords)), 0.0)

        # Iterate over each column (coordinate) in the dataframe
        for col_index in range(len(coords)):
            i, j = coords[col_index]  # Extract the row (i) and column (j) indices from the coordinates
            # Adding historical data for the predictor (pixel with indexes i,j) to the table
            dataframe[:-1, col_index] = self.training_data[:, i, j]
            # Entering the data of this pixel for the target matrix (last row of the dataframe)
            dataframe[-1, col_index] = self.temp_gap_array[i, j]

        dataframe = pd.DataFrame(dataframe)
        return dataframe

    def get_random_points(self):
        """
        Get random non-gap coordinates as predictors.

        This method selects random non-gap coordinates as predictors for machine learning models.
        It is used when the 'RandomPoints' predictor strategy is chosen.

        Returns:
        list or str: A list of random non-gap coordinates if there are enough known pixels;
                     otherwise, a message indicating insufficient known pixels.
        """
        n_strings = self.temp_gap_array.shape[0]
        n_columns = self.temp_gap_array.shape[1]
        coords = []

        # Determine the maximum number of iterations based on the absolute number of known pixels
        if self.temp_known_pixels >= 100:
            iter_value = 100  # Maximum number of iterations allowed (based on the absolute number of known pixels)
        elif self.temp_known_pixels >= 50:
            # Use all known non-gap coordinates
            if np.isnan(self.gap_value):
                coords = np.argwhere(~np.isnan(self.temp_gap_array))
            else:
                coords = np.argwhere(self.temp_gap_array != self.gap_value)
            return coords
        else:
            return "Not enough known pixels - proceed with interpolation"

        number_iter = 0  # Number of iterations
        # Randomly select coordinates until the maximum allowed iterations are reached
        while number_iter <= iter_value:
            random_i = random.randint(0, n_strings - 1)  # Generate a random row index
            random_j = random.randint(0, n_columns - 1)  # Generate a random column index
            coordinates = [random_i, random_j]  # Create a coordinate pair [row, column]

            # Check if the value at the random coordinate is a gap
            if np.isclose(self.temp_gap_array[random_i, random_j], -100) or np.isclose(self.temp_gap_array[random_i, random_j], np.nan):
                pass
            # Check if the coordinate already exists in the list
            elif any(tuple(coordinates) == tuple(element) for element in coords):
                pass
            else:
                coords.append(coordinates)  # Add the valid coordinate to the list
                number_iter += 1

        return coords

    def get_extra_matrix_points(self, gap_index):
        """
        Get coordinates based on the extra matrix (e.g. Land Cover Classification (LCC)) of the target pixel.

        This method selects coordinates based on the LCC of the target pixel to improve predictor selection.
        It ensures that predictors come from the same LCC biome as the target pixel.

        Parameters:
        - gap_index (tuple): The index (row, column) of the gap to be filled.

        Returns:
        list or str: A list of coordinates based on LCC if there are enough coordinates within the same biome;
                     otherwise, a message indicating insufficient known pixels.
        """
        extra_matrix = xr.open_dataset(self.directory + "extra_matrix_lcc.nc")['lccs_class'].to_numpy()

        # Extract the LCC value for the pixel to be filled
        extra_code = extra_matrix[gap_index[0], gap_index[1]]
        # Create a copy of the LCC matrix with cloud-covered areas set to gap values
        if np.isnan(self.gap_value):
            new_extra_matrix = np.where(np.isnan(self.temp_gap_array), self.gap_value, extra_matrix)
        else:
            new_extra_matrix = np.where(self.temp_gap_array == self.gap_value, self.gap_value, extra_matrix)

        # Find coordinates of points within the same LCC biome and not omitted
        coords = np.argwhere(new_extra_matrix == extra_code)

        # Check if there are less than 40 available coordinates within the same LCC
        if len(coords) < 40:
            # Get 100 random non-gap coordinates as predictors
            coords = self.get_random_points()
            # Check if there are enough random coordinates for modeling
            if type(coords) == str:
                return coords  # Return a message indicating insufficient known pixels

        # Calculate the distance from the target pixel to all other available coordinates
        target_pixel = np.array([[gap_index[0], gap_index[1]]])
        distances = scipy.spatial.distance.cdist(target_pixel, coords)[0]

        # Select the 40 nearest coordinates to the target pixel
        selected_coords = []
        for iter in range(min(40, len(coords))):
            # Find the index of the coordinate with the smallest distance from the target pixel
            index_min_dist = np.argmin(distances)
            # Get the coordinate of this pixel in the matrix
            new_coord = coords[index_min_dist]
            selected_coords.append(new_coord)
            # Replace the minimum element in the distances array with a very large number
            distances[index_min_dist] = np.inf

        # Add the index of the pixel for which the model is being built
        selected_coords.append([gap_index[0], gap_index[1]])

        return selected_coords

    def preprocess_dataframe(self, dataframe):
        """
        Preprocess the DataFrame by handling columns with gaps.

        This method preprocesses the DataFrame by removing columns with gaps in the target row.
        It ensures that the DataFrame is suitable for machine learning model training.

        Parameters:
        - dataframe (pd.DataFrame): The DataFrame containing historical data and target values.

        Returns:
        pd.DataFrame: The preprocessed DataFrame with columns that do not contain gaps in the target row.
        """
        # Extract the last row from the dataset (excluding the last element in it)
        last_row = np.array(dataframe.iloc[-1:, :-1])
        last_row = np.ravel(last_row)
        # Identify True where there are gaps in the last row
        last_row_na = np.ravel(np.isnan(last_row))
        # Get a list of column indexes that have gaps in the last row
        indexes_na = np.ravel(np.argwhere(last_row_na == True))
        # Convert indexes to strings for column manipulation
        indexes_na_str = [str(i) for i in indexes_na]

        # If there are gaps in the last row of the dataset, then delete the columns with gaps
        if len(indexes_na_str) > 0:
            for i in indexes_na_str:
                dataframe.drop([int(i)], axis=1, inplace=True)

            # Reset the column indexes after removing columns
            new_names = range(0, len(dataframe.columns))
            new = [str(i) for i in new_names]
            dataframe.set_axis(new, axis=1, copy=False)

        return dataframe

    def get_train_test_sets(self, dataframe):
        """
        Split the DataFrame into training and testing sets.

        This method splits the DataFrame into a training sample and an object to predict the value for.
        It excludes rows with gaps in the target row from the training sample.

        Parameters:
        - dataframe (pd.DataFrame): The preprocessed DataFrame containing historical data and target values.

        Returns:
        tuple: A tuple containing the following elements:
            1. X_train (ndarray): The training data (predictors).
            2. y_train (ndarray): The target values for training.
            3. X_test (ndarray): The data for which predictions are to be made.
        """
        # Divide the dataframe into a training sample and an object to predict the value for
        train = dataframe.iloc[:-1, :]
        test = dataframe.iloc[-1:, :]

        # We exclude all objects with omission in the target function from the training sample
        train = train.dropna()

        X_train = np.array(train.iloc[:, :-1])
        y_train = np.array(train.iloc[:, -1:])
        X_test = np.array(test.iloc[:, :-1])

        return X_train, y_train, X_test

    def process_results(self, gap_size, filled_array, actual_scores, validation_scores, start_time):
        """
        Process and print results for a specific gap size.

        This method processes the results of the gap filling for a specific gap size.
        It calculates and prints mean absolute error (MAE) for actual and cross-validation scores.

        Parameters:
        - gap_size (str): The size of the gap being filled.
        - filled_array (ndarray): The data array with gaps filled.
        - actual_scores (list of float): List of actual score values.
        - validation_scores (list of float): List of cross-validation score values.
        - start_time (float): The start time of the gap filling process.

        Returns:
        None
        """
        filled_xr_array = xr.DataArray(
            filled_array,
            dims=self.actual_matrix.dims,
            coords=self.actual_matrix.coords,
            attrs=self.actual_matrix.attrs
        )

        actual_date = np.datetime_as_string(self.actual_matrix.time, unit='D')
        filename = ''.join((actual_date, '-', str(gap_size), '.nc'))
        filled_xr_array.to_netcdf(self.directory + "Results/" + filename)

        # Calculate and round the mean absolute error (MAE) for actual scores if true matrix exists
        if os.path.exists(self.directory + "GapImitation/"):
            actual_scores = np.array(actual_scores)
            mean_actual_score = round(np.mean(actual_scores), 3)
            # Calculate and round the mean MAE for cross-validation scores
            try:
                validation_scores = np.array(validation_scores)
                mean_validation_score = round(np.mean(validation_scores), 3)
            except:
                mean_validation_score = "Could not be calculated as interpolation was partly used."

            self.scores[filename] = {"actual": mean_actual_score, "validation": mean_validation_score}
            print(f'MAE actual: {str(mean_actual_score)}')
            print(f'MAE cross validation: {mean_validation_score}')

        end_time = time.time()
        execution_time = end_time - start_time
        print(f"runtime: {execution_time:.2f} seconds \n")

    def train_model(self, X_train, y_train, X_test):
        """
        Perform machine learning model training and prediction.

        This method trains a machine learning model using the training data and predicts values for the test data.
        It handles different hyperparameter search methods and returns the predicted values and validation scores.

        Parameters:
        - X_train (ndarray): The training data (predictors).
        - y_train (ndarray): The target values for training.
        - X_test (ndarray): The data for which predictions are to be made.

        Returns:
        tuple: A tuple containing the following elements:
            1. predicted (float or ndarray): The predicted values for the test data.
            2. validation_score (float or str): The cross-validation score (if available).
        """
        # Performing gap filling using Support Vector Regression (SVR) as the predictive model.
        if self.learning_function == "SVR":
            # Combine sample for the standardization procedure
            sample = np.vstack((X_train, X_test))

            # Standardize the sample and split again
            sample = preprocessing.scale(sample)
            X_train = sample[:-1, :]
            X_test = sample[-1:, :]

            if self.hyperparameters == 'Custom':
                estimator = SVR()
                # Set the hyperparameters for the SVR model
                params = {'kernel': 'linear', 'gamma': 'scale', 'C': 1000, 'epsilon': 1}
                estimator.set_params(**params)

                # Perform cross-validation with 3 folds
                fold = KFold(n_splits=3, shuffle=True)
                try:
                    validation_score = cross_val_score(estimator=estimator, X=X_train, y=np.ravel(y_train), cv=fold,
                                                       scoring='neg_mean_absolute_error')
                except:
                    return None, None

                # Fit the SVR model on the training data and predict on the test data
                estimator.fit(X_train, np.ravel(y_train))
                predicted = estimator.predict(X_test)
            else:
                # Define lists of hyperparameter values for grid/randomized search
                Cs = [0.001, 0.01, 0.1, 1, 10]
                epsilons = [0.1, 0.4, 0.7, 1.0]
                param_grid = {'C': Cs, 'epsilon': epsilons}

                if self.hyperparameters == 'RandomGridSearch':
                    estimator = SVR(kernel='linear', gamma='scale', C=1000, epsilon=1)
                    # Perform randomized grid search with cross-validation (3 folds)
                    optimizer = RandomizedSearchCV(estimator, param_grid, n_iter=5, cv=3, scoring='neg_mean_absolute_error')
                elif self.hyperparameters == 'FullGridSearch':
                    estimator = SVR(kernel='linear', gamma='scale')
                    # Perform full grid search with cross-validation (3 folds)
                    optimizer = GridSearchCV(estimator, param_grid, cv=3, scoring='neg_mean_absolute_error')

                try:
                    # Fit the optimizer to the training data to find the best hyperparameters
                    optimizer.fit(X_train, np.ravel(y_train))
                except:
                    return None, None

                # Get the best SVR model from the optimizer
                regression = optimizer.best_estimator_
                # Predict using the best SVR model on the test data
                predicted = regression.predict(X_test)
                # Get the validation score (negative mean absolute error) from the optimizer
                validation_score = abs(optimizer.best_score_)

        return predicted, validation_score

    def interpolation(self, gap_index):
        """
        Interpolate gaps using nearest-neighbor interpolation.

        This method performs gap interpolation using nearest-neighbor interpolation method.

        Parameters:
        - gap_index (tuple): The index (row, column) of the gap to be filled.

        Returns:
        float: The interpolated value for the filled gap.
        """
        # Fill in gaps using the nearest neighbor interpolation
        all_pixels = self.temp_gap_array.size

        # Check if the matrix contains just gaps; if so, no interpolation is performed
        if all_pixels - (self.temp_gap_array == -100).sum() + np.isnan(self.temp_gap_array).sum() <= 10:
            print(f'No calculation for matrix - matrix contains just gaps')
        else:
            # Create a meshgrid of coordinates for the known values
            x, y = np.indices(self.temp_gap_array.shape)
            copy_matrix = np.copy(self.temp_gap_array)
            # Replace gap values with NaN to represent missing values
            copy_matrix[copy_matrix == self.gap_value] = np.nan
            # Extract coordinates and values of known pixels
            x_known = x[~np.isnan(copy_matrix)]
            y_known = y[~np.isnan(copy_matrix)]
            values_known = copy_matrix[~np.isnan(copy_matrix)]

            # Interpolate the missing value using nearest-neighbor method
            predicted = interpolate.griddata((x_known, y_known), values_known, (gap_index[0], gap_index[1]),
                                             method='nearest')

            return predicted